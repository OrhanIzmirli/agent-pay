export type Balance = { address: string; balance_sol: number };
export type Transaction = { signature: string; slot: number; err: unknown };
export type History = { address: string; transactions: Transaction[] };
export type DisputeStatus = 'none' | 'investigating' | 'escalated' | 'resolved';
export type Cover = 'drought' | 'excess_rain';
export type ProductType = 'crop_drought' | 'crop_excess_rain' | 'event_weather_cancel' | 'travel_delay';
export type Product = { product_type: ProductType; label: string; tagline: string; metric_unit: string; metric_label: string; direction: 'less' | 'more'; trigger: number; exit: number; sources: 'live' | 'demo'; source_note: string; icon_hint: string; period: string; subject: string; place_prompt: string; location_noun: string; payee_label: string; theme?: 'farm' | 'event' | 'flight'; venue_lookup?: boolean };
export type ProofReading = { source: string; value: number | string; unit: string; live: boolean; fetched_at: string; payout_ratio: number; calc: string };
export type Proof = { readings: ProofReading[]; trigger: number; exit: number; metric_unit: string; metric_label: string; product_type: ProductType; formula: string; floor_calc: string; ceiling_calc: string; tolerance: number; inputs_hash: string; ai_involvement: string; data_source_note: string | null };
export type PolicyInput = { region: string; lat: number; lon: number; trigger_mm: number; exit_mm: number; sum_insured_sol: number; payee_pubkey?: string; window_start?: string; window_end?: string; cover?: Cover; ndvi_trigger?: number; ndvi_exit?: number; field_polygon?: unknown; product_type?: ProductType; metric_unit?: string; metric_label?: string; venue_name?: string };
export type Policy = PolicyInput & { id: string; payee_pubkey: string; window_start: string; window_end: string; cover: Cover; product_type: ProductType; metric_unit: string; metric_label: string; ndvi_trigger: number; ndvi_exit: number; satellite_polygon_id: string | null; created_at: number; settled: boolean; last_evaluation: Evaluation | null; escrow: Escrow | null };
export type SourceReading = { source: string; observed_mm: number; live: boolean; detail: string; fetched_at: number; unit: string; payout_ratio: number | null; image_url: string | null; captured_at: string | null };
export type DisputeReport = { summary: string; suspected_cause: string; evidence: string[]; recommendation: 'auto_resolve' | 'escalate'; ai_used: boolean; model: string };
export type Escrow = { policy_id: string; payee: string; amount_sol: number; reason: string; status: 'pending' | 'released' | 'voided'; opened_at: number; resolved_at: number | null; resolved_by: 'human' | 'auto' | null; release_tx_signature: string | null; release_explorer_url: string | null; released_amount_sol?: number; voided_amount_sol?: number; onchain?: boolean; escrow_pda?: string; escrow_explorer_link?: string };
export type Evaluation = { policy_id: string; readings: SourceReading[]; payout_ratio_floor: number; payout_ratio_ceiling: number; floor_amount_sol: number; ceiling_amount_sol: number; escrow_amount_sol: number; floor_tx_signature: string | null; floor_explorer_url: string | null; dispute_status: DisputeStatus; dispute: DisputeReport | null; escrow: Escrow | null; elapsed_ms: number; spread: number; tolerance: number; note: string | null; cycle: 'fresh' | 'follow_up'; proof?: Proof; escrow_pda?: string | null; escrow_explorer_link?: string | null };
export type Resolution = { policy_id: string; dispute_status: DisputeStatus; released: boolean; escrow: Escrow; release_tx_signature: string | null; release_explorer_url: string | null };
// HTTP 202 from /evaluate or /resolve: the payment was submitted but its confirmation could not be established.
// It is neither a completed result nor an error: the money may already have moved, so the caller must only re-check.
export type Pending = { pending: true; status: 'pending_confirmation'; policy_id: string; tx_signature: string; floor_explorer_url: string | null; floor_amount_sol: number | null; message: string; escrow_pda?: string | null };
export const isPending = (x: unknown): x is Pending => typeof x === 'object' && x !== null && (x as { pending?: unknown }).pending === true;
export type SimulatedReading = { mm: number; label?: string };
export type EvaluateOptions = { simulate?: SimulatedReading[]; include_satellite?: boolean; simulate_satellite_ndvi?: number; simulate_event_status?: string };
const base = (import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? '/api' : 'https://containerapp-02.politestone-cbe80aa9.swedencentral.azurecontainerapps.io')).replace(/\/$/, '');
export const apiUrl = (path: string) => `${base}${path}`;
export const cluster = import.meta.env.VITE_SOLANA_CLUSTER || 'devnet';
export const explorer = (kind: 'tx' | 'address', id: string) => `https://explorer.solana.com/${kind}/${encodeURIComponent(id)}${cluster === 'mainnet-beta' ? '' : `?cluster=${encodeURIComponent(cluster)}`}`;
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options?.method === 'POST' ? 180000 : 20000);
  try {
    const response = await fetch(`${base}${path}`, { ...options, signal: controller.signal });
    if (!response.ok) {
      let detail = '';
      try { const body = await response.json(); detail = typeof body?.detail === 'string' ? body.detail : typeof body?.error === 'string' ? `${body.error}${body.detail ? `: ${body.detail}` : ''}` : ''; } catch { /* non-JSON error body */ }
      throw new Error(detail || `The service returned ${response.status}. Please try again later.`);
    }
    const data = await response.json();
    if (response.status === 202 && data?.status === 'pending_confirmation') return { ...data, pending: true } as T;
    return data as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new Error('The service took too long to respond.');
    if (error instanceof TypeError) throw new Error('Unable to reach the insurance service. Check your connection or try again shortly.');
    throw error;
  } finally { clearTimeout(timer); }
}
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export const createPolicy = (input: PolicyInput) => request<Policy>('/policy', json(input));
export const getPolicy = (id: string) => request<Policy>(`/policy/${encodeURIComponent(id)}`);
export const evaluatePolicy = (id: string, options: EvaluateOptions = {}) => request<Evaluation | Pending>(`/policy/${encodeURIComponent(id)}/evaluate`, json({ ...options, simulate: options.simulate?.length ? options.simulate : undefined }));
export const resolvePolicy = (id: string, release: boolean) => request<Resolution | Pending>(`/policy/${encodeURIComponent(id)}/resolve?release=${release}`, { method: 'POST' });
export const getProducts = () => request<{ formula: string; products: Product[] }>('/products');
