import { useEffect, useRef } from 'react';
import { ArrowUpRight, ArrowRight, X as XIcon } from 'lucide-react';
import { explorer } from './api';
import './landing.css';

// Marketing page for the pitch, on the Overview route. The Playground and Activity pages are
// untouched. Architecture: photo hero with the product name, a gridded cream page with big
// statements in cells, a line drawing that fills as you scroll, an orange block with three
// cards, a photo-and-numbers split, a marquee headline, the product mockup and a closing photo.
// Motion: column wipes between sections, scroll reveals, a count-up and a gentle photo parallax.
// Each section carries a data-theme (farm, event, flight, ai, close); the section in view sets the palette on .lp.
const PHOTO = '/hero-wheat.jpg';
const AERIAL = '/hero-aerial.jpg';
const WALLET = 'D93HiJbqXdt13pQxmehaqFvYGieRGrvXHxcVXt584N8B';
const Curtain = () => <div className="lp-curtain" aria-hidden="true"><i/><i/><i/><i/><i/></div>;

export default function Landing() {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    document.documentElement.classList.add('lp-light');
    const el = root.current!, vh = window.innerHeight;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const show = (t: HTMLElement) => {
      t.classList.add('in');
      t.querySelectorAll<HTMLElement>('[data-count]').forEach(n => {
        const end = Number(n.dataset.count), suffix = n.dataset.suffix ?? '', decimals = (n.dataset.count ?? '').split('.')[1]?.length ?? 0;
        if (reduced) { n.textContent = end.toFixed(decimals) + suffix; return; }
        const start = performance.now();
        const step = (now: number) => { const p = Math.min(1, (now - start) / 1100), e = 1 - Math.pow(1 - p, 3); n.textContent = (end * e).toFixed(decimals) + suffix; if (p < 1) requestAnimationFrame(step); };
        requestAnimationFrame(step);
      });
    };
    const targets = Array.from(el.querySelectorAll<HTMLElement>('[data-reveal]'));
    const pending = targets.filter(t => { if (t.getBoundingClientRect().top < vh * 0.9) { show(t); return false; } return true; });
    let io: IntersectionObserver | null = null;
    if (!reduced && 'IntersectionObserver' in window) { io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { show(e.target as HTMLElement); io!.unobserve(e.target); } }), { threshold: 0.12 }); pending.forEach(t => io!.observe(t)); }
    else pending.forEach(show);

    // Scroll-driven: the photo parallax.
    const photos = Array.from(el.querySelectorAll<HTMLElement>('.lp-photo'));
    let frame = 0;
    const tick = () => {
      frame = 0;
      for (const ph of photos) { const r = ph.parentElement!.getBoundingClientRect(); if (r.bottom < 0 || r.top > vh) continue; ph.style.transform = `translate3d(0, ${((r.top + r.height / 2 - vh / 2) * -0.1).toFixed(1)}px, 0) scale(1.12)`; }
    };
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(tick); };
    if (!reduced) { tick(); window.addEventListener('scroll', onScroll, { passive: true }); }
    // One gesture, one page. A wheel tick, arrow key or swipe moves to the next/previous section and
    // the browser smooth-scrolls there; further ticks during the glide are ignored.
    const pages = Array.from(el.querySelectorAll<HTMLElement>('.lp > section'));
    // Paging: `target` is the page we are heading to. A wheel move always steps from the target,
    // never from where the glide currently is, so rolling twice quickly reaches two pages further
    // with no waiting. Wheel events are grouped by short gaps (one mouse notch or one touchpad
    // flick is one group) so a flick's tail cannot count as a second move.
    let target = 0, lastWheel = 0, moved = false, touchY = 0;
    const current = () => { const y = window.scrollY + vh / 2; let i = 0; pages.forEach((p, k) => { if (p.offsetTop <= y) i = k; }); return i; };
    target = current();
    // Scroll-driven theme: whichever section current() says is in view sets the palette (sections carry data-theme).
    const applyTheme = () => { const t = pages[current()]?.dataset.theme ?? 'farm'; if (el.dataset.theme !== t) el.dataset.theme = t; };
    applyTheme(); window.addEventListener('scroll', applyTheme, { passive: true });
    const go = (dir: number) => { const i = Math.max(0, Math.min(pages.length - 1, target + dir)); if (i === target) return; target = i; window.scrollTo({ top: pages[i].offsetTop, behavior: reduced ? 'auto' : 'smooth' }); };
    const onWheel = (e: WheelEvent) => { if (e.ctrlKey) return; e.preventDefault(); const now = performance.now(); if (now - lastWheel > 120) moved = false; lastWheel = now; if (moved || Math.abs(e.deltaY) < 4) return; moved = true; go(e.deltaY > 0 ? 1 : -1); };
    const realign = () => { target = current(); const top = pages[target].offsetTop; if (Math.abs(window.scrollY - top) > 2) window.scrollTo({ top, behavior: 'auto' }); };
    window.addEventListener('resize', realign);
    const onKey = (e: KeyboardEvent) => { if (['ArrowDown', 'PageDown', ' '].includes(e.key)) { e.preventDefault(); go(1); } else if (['ArrowUp', 'PageUp'].includes(e.key)) { e.preventDefault(); go(-1); } };
    const onTouchStart = (e: TouchEvent) => { touchY = e.touches[0].clientY; };
    const onTouchEnd = (e: TouchEvent) => { const d = touchY - e.changedTouches[0].clientY; if (Math.abs(d) > 40) go(d > 0 ? 1 : -1); };
    const pagerOn = window.innerWidth > 900;
    if (pagerOn) { window.addEventListener('wheel', onWheel, { passive: false }); window.addEventListener('keydown', onKey); window.addEventListener('touchstart', onTouchStart, { passive: true }); window.addEventListener('touchend', onTouchEnd); }
    return () => { io?.disconnect(); window.removeEventListener('scroll', onScroll); window.removeEventListener('scroll', applyTheme); cancelAnimationFrame(frame); document.documentElement.classList.remove('lp-light'); if (pagerOn) { window.removeEventListener('wheel', onWheel); window.removeEventListener('keydown', onKey); window.removeEventListener('resize', realign); window.removeEventListener('touchstart', onTouchStart); window.removeEventListener('touchend', onTouchEnd); } };
  }, []);

  return <div className="lp" ref={root}>

    {/* 1. Hero */}
    <section className="lp-hero lp-bleed" data-theme="farm">
      <div className="lp-photo" style={{ backgroundImage: `url(${AERIAL})`, backgroundPosition: 'center 50%' }} aria-hidden="true"/>
      <div className="lp-shade" aria-hidden="true"/>
      <div className="lp-hero-top"><h1 className="lp-rise">Agent Pay</h1><a className="lp-pill lp-rise lp-d1" href="#playground">Open the playground</a></div>
      <div className="lp-hero-rule lp-rise lp-d1"><span>Crop insurance, reinvented.</span><span className="lp-sol"><svg className="sol-mark" viewBox="0 0 24 20" aria-hidden="true"><path d="M4.6 0h19.4l-4.6 4.8H0z"/><path d="M4.6 7.6h19.4L19.4 12.4H0z"/><path d="M4.6 15.2h19.4L19.4 20H0z"/></svg> Built on Solana · every payout is a real transaction</span><span>A claim settled in seconds,<br/>not a season.</span></div>
    </section>

    {/* 2. Statement in the grid */}
    <section className="lp-grid lp-statement lp-hue" data-theme="farm"><Curtain/>
      <div className="lp-cells">
        <div className="lp-cell lp-c1 lp-r2" data-reveal><span className="lp-big">Most claims</span></div>
        <div className="lp-cell lp-c2 lp-r2 lp-photo-cell" data-reveal style={{ ['--d' as string]: '120ms' }}><span className="lp-mosaic"><i style={{ backgroundImage: `url(${PHOTO})`, backgroundPosition: '30% 60%' }}/><i style={{ backgroundImage: 'url(/event-rain.jpg)' }}/><i style={{ backgroundImage: 'url(/flight-delay.jpg)', backgroundPosition: '85% 40%' }}/><i style={{ backgroundImage: 'url(/transport-delay.jpg)', backgroundPosition: '68% 35%' }}/></span></div>
        <div className="lp-cell lp-c3 lp-r2 lp-span2" data-reveal style={{ ['--d' as string]: '200ms' }}><span className="lp-big">take a season<br/>to pay out</span></div>
        <div className="lp-cell lp-c4 lp-r3" data-reveal style={{ ['--d' as string]: '320ms' }}><p>A loss report, an adjuster visit, a paper trail. The farmer waits with it, often until the next planting. Agent Pay reads the weather and the field instead, and pays the moment the numbers cross the line.</p></div>
      </div>
    </section>

    {/* 5. Photo + numbers */}
    <section className="lp-splitnum" data-theme="farm">
      <div className="lp-splitnum-photo"><div className="lp-photo" style={{ backgroundImage: 'url(/farmer.jpg)', backgroundPosition: '50% 60%' }} aria-hidden="true"/></div>
      <div className="lp-numbers" data-reveal>
        <div><b data-count="2">0</b><span>independent weather models, read for every policy</span></div>
        <div><b data-count="1">0</b><span>satellite crop-health index from Sentinel-2 and Landsat</span></div>
        <div><b data-count="3">0</b><span>cross-checks before a single lamport moves</span></div>
        <div><b data-count="100" data-suffix="%">0%</b><span>of payouts settled as real transactions on Solana</span></div>
        <div className="lp-num-sol"><b><svg className="sol-mark" viewBox="0 0 24 20" aria-hidden="true"><path d="M4.6 0h19.4l-4.6 4.8H0z"/><path d="M4.6 7.6h19.4L19.4 12.4H0z"/><path d="M4.6 15.2h19.4L19.4 20H0z"/></svg></b><span>runs on Solana devnet · about a second per payout, a fraction of a cent in fees · <a href={explorer('address', WALLET)} target="_blank" rel="noreferrer">see the wallet</a></span></div>
        <p className="lp-foot-note">Sources are compared automatically. When they agree, the formula pays in full. When they disagree, the floor is paid now and only the difference waits for review.</p>
      </div>
      <div className="lp-marquee" aria-hidden="true"><div className="lp-marquee-track">{Array.from({ length: 4 }, (_, i) => <span key={i}>For the field. For the farmer. For the formula. </span>)}</div></div>
    </section>

    {/* 7. Product */}
    <section className="lp-grid lp-product" data-theme="farm">
      <div className="lp-product-head" data-reveal><span className="lp-big">See the field.<br/>See the money.</span><p>Pick a cover, point at the field, and watch the payout settle. Every transaction is real, on Solana devnet.</p></div>
      <div className="lp-shot lp-mock" data-reveal>
        <div className="lp-shot-bar"><i/><i/><i/><span>agentpay · playground</span></div>
        <div className="mock-body">
          <div className="mock-verdict"><span className="mock-big"><ArrowUpRight size={22}/></span><div><b>You got paid.</b><p>0.0017 SOL is in your wallet, and 0.0083 SOL more may follow.</p></div></div>
          <ol className="mock-chain">
            <li className="mock-box"><small>1 · Rain this week</small><b>34 mm · 35 mm</b><span>reports differ · field from space: looks bad</span></li>
            <li className="mock-arrow" aria-hidden="true"><ArrowRight size={20}/></li>
            <li className="mock-box mock-win"><small>2 · Paid to you now</small><b>0.0017 SOL</b><span>already in your wallet</span></li>
            <li className="mock-arrow" aria-hidden="true"><ArrowRight size={20}/></li>
            <li className="mock-box mock-wait"><small>3 · Still waiting</small><b>0.0083 SOL</b><span>a person must decide</span></li>
          </ol>
          <div className="mock-decide"><p>What should happen to the 0.0083 SOL that is waiting?</p><div><span className="mock-btn mock-yes"><ArrowUpRight size={14}/> Pay it to me</span><span className="mock-btn mock-no"><XIcon size={14}/> Don't pay it</span></div></div>
          <div className="mock-why"><b>Why part of it waits</b><p>The weather reports say one thing, the satellite picture of your field says another. We paid what all of them agree on. The rest waits until someone checks the picture.</p></div>
        </div>
        <div className="lp-shot-foot"><span>The real Playground, one evaluation later.</span><a href="#playground?product=crop_drought">Open it yourself</a></div>
      </div>
    </section>

    {/* 7a. Statement in the grid: the event cover (same shape as section 2) */}
    <section className="lp-grid lp-statement" data-theme="event"><Curtain/>
      <div className="lp-cells">
        <div className="lp-cell lp-c1 lp-r2" data-reveal><span className="lp-big">A show<br/>rained out</span></div>
        <div className="lp-cell lp-c2 lp-r2 lp-photo-cell" data-reveal style={{ ['--d' as string]: '120ms' }}><span style={{ backgroundImage: 'url(/event-rain.jpg)', backgroundPosition: '42% 40%' }}/></div>
        <div className="lp-cell lp-c3 lp-r2 lp-span2" data-reveal style={{ ['--d' as string]: '200ms', flexDirection: 'column', alignItems: 'flex-start', justifyContent: 'center', gap: 26 }}><span className="lp-big">refunded before<br/>the crowd goes home</span><a className="lp-pill lp-pill-solid" href="#playground?product=event_weather_cancel">Open the playground</a></div>
        <div className="lp-cell lp-c4 lp-r3" data-reveal style={{ ['--d' as string]: '320ms' }}><p>The stage is built, the crew is booked, and the forecast turns. Agent Pay reads the rain over the event window and pays the organiser once it crosses the line. If the ticketing system says the show is cancelled, that counts as a second opinion.</p></div>
      </div>
    </section>

    {/* 7b. Photo + numbers: the delay cover (same shape as section 5) */}
    <section className="lp-splitnum" data-theme="flight">
      <div className="lp-splitnum-photo"><div className="lp-photo" style={{ backgroundImage: 'url(/flight-delay.jpg)', backgroundPosition: '92% 40%' }} aria-hidden="true"/></div>
      <div className="lp-numbers" data-reveal>
        <div><b data-count="2">0</b><span>independent delay feeds, compared for every journey</span></div>
        <div><b data-count="30">0</b><span>minutes late before the payout starts</span></div>
        <div><b data-count="180">0</b><span>minutes late, and the full cover is paid</span></div>
        <div><b data-count="100" data-suffix="%">0%</b><span>of payouts settled as real transactions on Solana</span></div>
        <p className="lp-foot-note">When the feeds agree, the formula pays in full. When they disagree, the floor is paid now and only the difference waits for review. The delay feeds are simulated for now; the settlement is real.</p>
        <a className="lp-pill lp-pill-solid" href="#playground?product=travel_delay" style={{ alignSelf: 'flex-start', marginTop: 22 }}>Open the playground</a>
      </div>
      <div className="lp-marquee" aria-hidden="true"><div className="lp-marquee-track">{Array.from({ length: 4 }, (_, i) => <span key={i}>For the traveller. For the delay. For the formula. </span>)}</div></div>
    </section>

    {/* 7c. The watchdog, said plainly (statement grid shape, text only) */}
    <section className="lp-grid lp-statement" data-theme="ai"><Curtain/>
      <div className="lp-cells">
        <div className="lp-cell lp-c1 lp-r2" data-reveal><span className="lp-big">The formula</span></div>
        <div className="lp-cell lp-c2 lp-r2" data-reveal style={{ ['--d' as string]: '120ms' }}><span className="lp-big">sets the<br/>amount.</span></div>
        <div className="lp-cell lp-c3 lp-r2 lp-span2" data-reveal style={{ ['--d' as string]: '200ms' }}><span className="lp-big">The AI only<br/>explains why.</span></div>
        <div className="lp-cell lp-c4 lp-r3" data-reveal style={{ ['--d' as string]: '320ms' }}><p>When two sources disagree, an AI watchdog writes down what it sees for a human reviewer. It never sets or changes an amount, releases the escrow or sends a payment. Only a person can.</p></div>
      </div>
    </section>

    {/* 8. Closing */}
    <section className="lp-close" data-theme="close">
      <div className="lp-close-inner" data-reveal>
        <h2>Be first to cover<br/>a field this way.</h2>
        <p>Create a policy, force a disagreement, and watch the floor pay while the dispute waits.</p>
        <div className="lp-actions"><a className="lp-pill lp-pill-light" href="#playground">Open the playground</a><a className="lp-pill" href={explorer('address', WALLET)} target="_blank" rel="noreferrer">Watch the wallet <ArrowUpRight size={14}/></a></div>
      </div>
      <div className="lp-close-foot"><span>Agent Pay</span><span>Parametric cover. Deterministic payouts.</span><span className="lp-sol"><svg className="sol-mark" viewBox="0 0 24 20" aria-hidden="true"><path d="M4.6 0h19.4l-4.6 4.8H0z"/><path d="M4.6 7.6h19.4L19.4 12.4H0z"/><path d="M4.6 15.2h19.4L19.4 20H0z"/></svg> Hackathon prototype · Solana devnet</span></div>
    </section>
  </div>;
}
