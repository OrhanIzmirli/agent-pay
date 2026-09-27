//! Agent Pay vault: the disputed "delta" of a parametric payout sits in a program-owned PDA
//! until the insurer's authority releases it to the payee or voids it back to itself.
//! Funds can only leave through `release_escrow` / `void_escrow`, both of which require the
//! authority that opened the escrow to sign and the escrow to still be Held.

use anchor_lang::prelude::*;
use anchor_lang::system_program::{transfer, Transfer};

declare_id!("CvE7xMfwbpMmCz9Pvt6RsG9tHxNUpbuG5KwyGuFHSxCk");

pub const STATUS_HELD: u8 = 0;
pub const STATUS_RELEASED: u8 = 1;
pub const STATUS_VOIDED: u8 = 2;

#[program]
pub mod agent_pay_vault {
    use super::*;

    /// Creates the escrow PDA (rent paid by `authority`) and locks `amount` lamports in it.
    pub fn initialize_escrow(ctx: Context<InitializeEscrow>, policy_id: [u8; 32], amount: u64, payee: Pubkey) -> Result<()> {
        require!(amount > 0, VaultError::ZeroAmount);
        let escrow = &mut ctx.accounts.escrow;
        escrow.policy_id = policy_id;
        escrow.payee = payee;
        escrow.amount = amount;
        escrow.authority = ctx.accounts.authority.key();
        escrow.status = STATUS_HELD;
        escrow.bump = ctx.bumps.escrow;
        transfer(
            CpiContext::new(
                ctx.accounts.system_program.to_account_info(),
                Transfer { from: ctx.accounts.authority.to_account_info(), to: ctx.accounts.escrow.to_account_info() },
            ),
            amount,
        )
    }

    /// Pays the held lamports to `payee` and marks the escrow Released. `amount` may be less
    /// than the held total (a partial release): the remainder goes back to the authority.
    pub fn release_escrow(ctx: Context<Resolve>, amount: u64) -> Result<()> {
        let held = ctx.accounts.escrow.amount;
        require!(amount <= held, VaultError::AmountExceedsHeld);
        move_lamports(&ctx.accounts.escrow.to_account_info(), &ctx.accounts.payee.to_account_info(), amount)?;
        move_lamports(&ctx.accounts.escrow.to_account_info(), &ctx.accounts.authority.to_account_info(), held - amount)?;
        ctx.accounts.escrow.status = STATUS_RELEASED;
        Ok(())
    }

    /// Returns the held lamports to the authority and marks the escrow Voided.
    pub fn void_escrow(ctx: Context<Resolve>) -> Result<()> {
        let held = ctx.accounts.escrow.amount;
        move_lamports(&ctx.accounts.escrow.to_account_info(), &ctx.accounts.authority.to_account_info(), held)?;
        ctx.accounts.escrow.status = STATUS_VOIDED;
        Ok(())
    }
}

/// Direct lamport move out of a program-owned account (a system transfer cannot spend it).
/// Only the escrow's held amount moves; its rent-exempt reserve stays so the record survives.
fn move_lamports(from: &AccountInfo, to: &AccountInfo, amount: u64) -> Result<()> {
    if amount == 0 {
        return Ok(());
    }
    **from.try_borrow_mut_lamports()? = from.lamports().checked_sub(amount).ok_or(VaultError::AmountExceedsHeld)?;
    **to.try_borrow_mut_lamports()? = to.lamports().checked_add(amount).ok_or(VaultError::AmountExceedsHeld)?;
    Ok(())
}

#[derive(Accounts)]
#[instruction(policy_id: [u8; 32])]
pub struct InitializeEscrow<'info> {
    #[account(init, payer = authority, space = 8 + EscrowAccount::INIT_SPACE, seeds = [b"escrow", policy_id.as_ref()], bump)]
    pub escrow: Account<'info, EscrowAccount>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Resolve<'info> {
    #[account(
        mut,
        seeds = [b"escrow", escrow.policy_id.as_ref()],
        bump = escrow.bump,
        has_one = authority @ VaultError::NotAuthority,
        constraint = escrow.status == STATUS_HELD @ VaultError::NotHeld,
    )]
    pub escrow: Account<'info, EscrowAccount>,
    #[account(mut)]
    pub authority: Signer<'info>,
    /// CHECK: must equal the payee recorded in the escrow; only receives lamports.
    #[account(mut, address = escrow.payee)]
    pub payee: UncheckedAccount<'info>,
}

#[account]
#[derive(InitSpace)]
pub struct EscrowAccount {
    pub policy_id: [u8; 32],
    pub payee: Pubkey,
    pub amount: u64,
    pub authority: Pubkey,
    pub status: u8, // 0 Held, 1 Released, 2 Voided
    pub bump: u8,
}

#[error_code]
pub enum VaultError {
    #[msg("Only the escrow authority can do this")]
    NotAuthority,
    #[msg("Escrow is not in the Held state")]
    NotHeld,
    #[msg("Amount must be positive")]
    ZeroAmount,
    #[msg("Amount exceeds the held escrow")]
    AmountExceedsHeld,
}
