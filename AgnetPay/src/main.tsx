import { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUpRight, ArrowRight, ArrowDown, Check, Copy, RefreshCw, Wallet, Sparkles, ChevronRight, AlertCircle, Zap, Lock, ShieldAlert, CloudRain, Satellite, MapPin, Hourglass, Sun, Umbrella, Plane, SlidersHorizontal, Package, X as XIcon, type LucideIcon } from 'lucide-react';
import { request, explorer, cluster, apiUrl, createPolicy, evaluatePolicy, resolvePolicy, getProducts, isPending, type Pending, type Resolution, type Product, type Balance, type History, type Policy, type Evaluation, type EvaluateOptions, type Cover, type ProductType } from './api';
import Landing from './Landing';
import './styles.css';
import './workspace.css';

const short = (value: string) => `${value.slice(0, 5)}…${value.slice(-5)}`;
const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong.';
const sol = (n: number) => `${n.toLocaleString('en-US', { maximumFractionDigits: 9 })} SOL`;
const pct = (r: number) => `${(r * 100).toFixed(1)}%`;
const statusLabel = { none: 'None · sources agree', investigating: 'Investigating', escalated: 'Escalated to reviewer', resolved: 'Resolved' } as const;
// Plain-language versions for the Playground result.
const plainStatus = { none: 'Settled', investigating: 'Waiting for the next reading', escalated: 'Waiting for a reviewer', resolved: 'Settled' } as const;
const scenarioCopy: Record<string, { title: string; text: string }> = {
  'Live Open-Meteo readings': { title: 'Live weather', text: 'Two real forecast models for last week over the field.' },
  'Force agreement': { title: 'Sources agree', text: 'Both models report about the same rain. Full payout, no review.' },
  'Force disagreement': { title: 'Sources disagree', text: 'One model says wet, one says dry. The minimum is paid now, the rest waits.' },
  'Satellite conflict': { title: 'Satellite says otherwise', text: 'The weather looks fine, but the field looks stressed from space.' },
  'On time': { title: 'On time', text: 'Both delay feeds report a few minutes. Nothing is owed.' },
  'Long delay': { title: 'Long delay', text: 'Both feeds agree the train was hours late. Big payout, no review.' },
  'Feeds disagree': { title: 'Feeds disagree', text: 'One feed says 20 minutes, the other 2.5 hours. The minimum is paid, the rest waits.' },
};
const sol4 = (n: number) => `${n.toFixed(4)} SOL`;
const sourceName = (s: string) => s.includes('delay-feed-A') ? 'Delay feed A' : s.includes('delay-feed-B') ? 'Delay feed B' : s.includes('best_match') || s.endsWith(':A') ? 'Weather model A' : s.includes('ecmwf') || s.endsWith(':B') ? 'Weather model B' : s.includes('ndvi') || s.includes('satellite') ? 'Satellite' : s;
const verdict = (ratio: number | null) => ratio == null ? '' : ratio <= 0.01 ? 'No loss' : ratio < 0.5 ? `Some loss · ${pct(ratio)}` : ratio < 0.99 ? `Heavy loss · ${pct(ratio)}` : 'Total loss';
const providerBadge = (d: { ai_used: boolean; model: string } | null | undefined) => !d ? '' : !d.ai_used ? 'Rule-based check' : d.model.toLowerCase().startsWith('groq') ? 'Explained by Groq AI' : d.model.toLowerCase().includes('claude') ? 'Explained by Claude AI' : `Explained by ${d.model}`;
function headline(r: Evaluation): { title: string; sub: string } {
  if (r.dispute_status === 'none') return { title: `${sol4(r.floor_amount_sol)} paid.`, sub: r.floor_amount_sol > 0 ? 'Every source agrees. The full amount went out on-chain. Nothing is waiting.' : 'Every source agrees: your rule was not hit. Nothing is owed.' };
  if (r.dispute_status === 'resolved') return { title: `${sol4(r.floor_amount_sol)} paid now.`, sub: r.escrow?.status === 'released' ? `The held ${sol4(r.escrow.released_amount_sol ?? r.escrow.amount_sol)} was released too. Done.` : 'The held amount was voided. Done.' };
  return { title: `${sol4(r.floor_amount_sol)} paid now.`, sub: `The sources don't agree, so ${sol4(r.escrow_amount_sol)} is held ${r.dispute_status === 'escalated' ? 'until a reviewer decides' : 'until the next reading'}.` };
}type Form = { region: string; lat: string; lon: string; trigger_mm: string; exit_mm: string; sum_insured_sol: string; payee_pubkey: string; venue_name: string; event_date: string; ndvi_trigger: string; ndvi_exit: string };
const defaultForm: Form = { region: 'Warsaw, PL', lat: '52.23', lon: '21.01', trigger_mm: '40', exit_mm: '10', sum_insured_sol: '0.01', payee_pubkey: '', venue_name: '', event_date: '', ndvi_trigger: '0.55', ndvi_exit: '0.25' };
// The product picker, metric labels, units and copy all read off the GET /products catalog: a new
// vertical is a new catalog entry, not new JSX. 'custom' is the only client-side option.
type Metric = { unit: string; label: string; noun: string; period: string; who: string; sources: string };
const CUSTOM = 'custom';
const iconFor: Record<string, LucideIcon> = { sun: Sun, 'cloud-rain': CloudRain, umbrella: Umbrella, plane: Plane, sliders: SlidersHorizontal };
const metricOf = (p?: Product): Metric => { const label = p?.metric_label ?? 'Rainfall', noun = label.toLowerCase(); return { unit: p?.metric_unit ?? 'mm', label, noun, period: p?.period ?? 'this week', who: `Who tells us the ${noun}?`, sources: `${noun} readings` }; };
const coverOf = (p: Product): Cover => p.direction === 'less' ? 'drought' : 'excess_rain';
type ReadingsMode = 'live' | 'simulate';
const presets: { label: string; mode: ReadingsMode; a?: string; b?: string; ndvi?: string }[] = [{ label: 'Live Open-Meteo readings', mode: 'live' }, { label: 'Force agreement', mode: 'simulate', a: '24', b: '25.5' }, { label: 'Force disagreement', mode: 'simulate', a: '30', b: '12' }, { label: 'Satellite conflict', mode: 'simulate', a: '34', b: '35', ndvi: '0.20' }];
const travelPresets: { label: string; mode: ReadingsMode; a?: string; b?: string; ndvi?: string }[] = [{ label: 'On time', mode: 'simulate', a: '8', b: '12' }, { label: 'Long delay', mode: 'simulate', a: '140', b: '150' }, { label: 'Feeds disagree', mode: 'simulate', a: '20', b: '150' }];
const unitValue = (r: { observed_mm: number; unit: string }) => r.unit === 'ndvi' ? `NDVI ${r.observed_mm.toFixed(2)}` : `${r.observed_mm.toFixed(1)} ${r.unit}`;
const tilt = (e: React.MouseEvent<HTMLElement>) => { const el = e.currentTarget, r = el.getBoundingClientRect(); const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5; el.style.setProperty('--ry', `${(x * 6).toFixed(2)}deg`); el.style.setProperty('--rx', `${(-y * 6).toFixed(2)}deg`); };
const untilt = (e: React.MouseEvent<HTMLElement>) => { e.currentTarget.style.setProperty('--rx', '0deg'); e.currentTarget.style.setProperty('--ry', '0deg'); };
// Client-side preview of the same formula the backend runs, so each step can say what you'd earn.
// Hover/focus explanation next to a field label, for people who have never seen a parametric policy.
const Hint = ({ text }: { text: string }) => <span className="hint" tabIndex={0} role="note" aria-label={text}><i>?</i><span className="hint-pop">{text}</span></span>;
const ratioOf = (obs: number, trig: number, ex: number) => trig === ex ? 0 : Math.max(0, Math.min(1, (trig - obs) / (trig - ex)));
// '#playground?product=event_weather_cancel' -> { route: 'playground', product: 'event_weather_cancel' }
const parseHash = () => { const [route, query = ''] = location.hash.slice(1).split('?'); return { route: route || 'overview', product: new URLSearchParams(query).get('product') }; };
function App() {
  const [page, setPage] = useState(parseHash().route); const [hashProduct, setHashProduct] = useState<string | null>(parseHash().product); useEffect(() => { const change = () => { const h = parseHash(); setPage(h.route); setHashProduct(h.product); }; window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []); const [balance, setBalance] = useState<Balance | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [balanceError, setBalanceError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [form, setForm] = useState<Form>(defaultForm);
  const [preset, setPreset] = useState<string>('crop_drought');
  const [catalog, setCatalog] = useState<Product[]>([]); const [catalogError, setCatalogError] = useState('');
  useEffect(() => { getProducts().then(r => setCatalog(r.products)).catch(e => setCatalogError(message(e))); }, []);
  const [cover, setCover] = useState<Cover>('drought');
  const [mode, setMode] = useState<ReadingsMode>('live');
  const [simA, setSimA] = useState('30');
  const [simB, setSimB] = useState('12');
  const [satellite, setSatellite] = useState(false);
  const [step, setStep] = useState(1);
  const [simNdvi, setSimNdvi] = useState('');
  const [simEventStatus, setSimEventStatus] = useState('');
  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState('');
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [result, setResult] = useState<Evaluation | null>(null);
  // A 202 pending_confirmation: the payment may already have landed. Only ever re-check it, never resend.
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingAction, setPendingAction] = useState<{ kind: 'evaluate' } | { kind: 'resolve'; release: boolean }>({ kind: 'evaluate' });
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const busy = useRef(false);
  const refreshingRef = useRef(false);
  // The pill nav compacts once the page is scrolled.
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 48);
    onScroll(); window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, [page]);
  const refresh = useCallback(async () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true; setRefreshing(true);
    await Promise.allSettled([
      request<Balance>('/wallet/balance').then(data => { setBalance(data); setBalanceError(''); }).catch(e => setBalanceError(message(e))),
      request<History>('/wallet/history').then(data => { setHistory(data); setHistoryError(''); }).catch(e => setHistoryError(message(e))),
    ]);
    refreshingRef.current = false; setRefreshing(false);
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!running) return;
    const start = Date.now(); const interval = setInterval(() => setElapsed((Date.now() - start) / 1000), 100);
    return () => clearInterval(interval);
  }, [running]);
  useEffect(() => { if (copied) { const timer = setTimeout(() => setCopied(false), 2000); return () => clearTimeout(timer); } }, [copied]);
  const num = (v: string) => { const n = Number(v); return v.trim() !== '' && Number.isFinite(n) ? n : NaN; };
  const trigger = num(form.trigger_mm), exit = num(form.exit_mm), sum = num(form.sum_insured_sol), ndviT = num(form.ndvi_trigger), ndviE = num(form.ndvi_exit);
  const directionOk = cover === 'drought' ? exit >= 0 && exit < trigger : trigger >= 0 && exit > trigger;
  const ndviOk = simNdvi.trim() === '' || (num(simNdvi) >= -1 && num(simNdvi) <= 1);
  const formValid = form.region.trim() !== '' && Number.isFinite(num(form.lat)) && Number.isFinite(num(form.lon)) && trigger > 0 && directionOk && sum > 0 && ndviE < ndviT && ndviE > -1 && ndviT < 1 && (mode === 'live' || (num(simA) >= 0 && num(simB) >= 0 && ndviOk));
  type Fixed = { a: number; b: number };  // the quick demo's own readings (state set in the same tick would still be stale)
  const evalOptions = (fx?: Fixed): EvaluateOptions => ({ simulate: fx || mode === 'simulate' ? [{ mm: fx ? fx.a : num(simA), label: isDemo ? 'delay-feed-A' : 'A' }, { mm: fx ? fx.b : num(simB), label: isDemo ? 'delay-feed-B' : 'B' }] : undefined, include_satellite: fx ? false : satellite, simulate_satellite_ndvi: !fx && mode === 'simulate' && simNdvi.trim() !== '' ? num(simNdvi) : undefined, simulate_event_status: !fx && mode === 'simulate' && currentProduct?.venue_lookup && simEventStatus ? simEventStatus : undefined });
  const currentProduct = preset === CUSTOM ? undefined : catalog.find(p => p.product_type === preset);
  const M = metricOf(currentProduct);
  const isDemo = preset !== CUSTOM && currentProduct?.sources === 'demo';
  const place = currentProduct?.location_noun ?? 'location';
  const payeeLabel = currentProduct?.payee_label ?? 'Your wallet';
  const theme = currentProduct?.theme ?? (preset === CUSTOM ? 'custom' : 'farm');   // same themes as the Landing page, read off the catalog
  const activePresets = isDemo ? travelPresets : presets;
  const set = (key: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm(f => ({ ...f, [key]: e.target.value }));
  const choosePreset = (key: string) => { const p = catalog.find(c => c.product_type === key); setPreset(key); setSimEventStatus(''); if (p) { setCover(coverOf(p)); setForm(f => ({ ...f, trigger_mm: String(p.trigger), exit_mm: String(p.exit) })); } else { setCover('drought'); setForm(f => ({ ...f, trigger_mm: defaultForm.trigger_mm, exit_mm: defaultForm.exit_mm, ndvi_trigger: defaultForm.ndvi_trigger, ndvi_exit: defaultForm.ndvi_exit })); } if (p?.sources === 'demo') { setMode('simulate'); setSimA('20'); setSimB('150'); setSimNdvi(''); setSatellite(false); } else if (mode === 'simulate' && (simA === '20' && simB === '150')) { setMode('live'); } };
  const chooseCover = (c: Cover) => { setCover(c); setForm(f => { const t = num(f.trigger_mm), x = num(f.exit_mm); const ok = c === 'drought' ? x < t : x > t; return ok ? f : { ...f, trigger_mm: c === 'drought' ? '40' : '80', exit_mm: c === 'drought' ? '10' : '140' }; }); };
  // Deep link from the Landing page: #playground?product=<product_type> lands on Step 1 with that product selected.
  const appliedProduct = useRef<string | null>(null);
  useEffect(() => {
    if (page !== 'playground' || !hashProduct) { appliedProduct.current = null; return; }
    if (!catalog.length || appliedProduct.current === hashProduct) return;
    appliedProduct.current = hashProduct;
    if (catalog.some(c => c.product_type === hashProduct)) { setResult(null); setPending(null); setPolicy(null); setError(''); choosePreset(hashProduct); setStep(1); }
  }, [page, hashProduct, catalog]);
  async function run(fx?: Fixed) {
    if ((!fx && !formValid) || busy.current) return;
    busy.current = true; setRunning(true); setError(''); setResult(null); setPending(null); setPolicy(null); setElapsed(0);
    try {
      setPhase('Creating policy');
      const eventWindow = currentProduct?.venue_lookup && form.event_date ? form.event_date : undefined;
      const created = await createPolicy({ region: form.region.trim(), lat: num(form.lat), lon: num(form.lon), trigger_mm: trigger, exit_mm: exit, sum_insured_sol: sum, payee_pubkey: form.payee_pubkey.trim() || undefined, cover, ndvi_trigger: ndviT, ndvi_exit: ndviE, product_type: currentProduct?.product_type ?? 'crop_drought', metric_unit: M.unit, metric_label: M.label, venue_name: currentProduct?.venue_lookup && form.venue_name.trim() ? form.venue_name.trim() : undefined, window_start: eventWindow, window_end: eventWindow });
      setPolicy(created);
      setPhase(!fx && mode === 'live' ? `Pulling two rainfall sources${satellite ? ' and satellite NDVI' : ''}, then paying the floor` : 'Applying simulated readings and paying the floor');
      const out = await evaluatePolicy(created.id, evalOptions(fx));
      if (isPending(out)) { setPendingAction({ kind: 'evaluate' }); setPending(out); } else setResult(out);
    }
    catch (e) { setError(`${message(e)} The outcome may be unknown. Check recent transactions before submitting again.`); }
    finally { busy.current = false; setRunning(false); setPhase(''); void refresh(); }
  }
  async function nextCycle() {
    if (!policy || busy.current) return;
    busy.current = true; setRunning(true); setError(''); setElapsed(0); setPhase('Re-pulling readings for the next cycle');
    try { const out = await evaluatePolicy(policy.id, evalOptions()); if (isPending(out)) { setPendingAction({ kind: 'evaluate' }); setPending(out); } else setResult(out); }
    catch (e) { setError(message(e)); }
    finally { busy.current = false; setRunning(false); setPhase(''); void refresh(); }
  }
  async function resolve(release: boolean) {
    if (!policy || busy.current) return;
    busy.current = true; setRunning(true); setError(''); setElapsed(0); setPhase(release ? 'Releasing the escrowed delta on-chain' : 'Voiding the escrowed delta');
    try { const res = await resolvePolicy(policy.id, release); if (isPending(res)) { setPendingAction({ kind: 'resolve', release }); setPending(res); } else applyResolution(res); }
    catch (e) { setError(message(e)); }
    finally { busy.current = false; setRunning(false); setPhase(''); void refresh(); }
  }
  const applyResolution = (res: Resolution) => setResult(prev => prev ? { ...prev, dispute_status: res.dispute_status, escrow: res.escrow, note: `Escrow ${res.escrow.status} by a human reviewer.` } : prev);
  // "Check again" for a pending payment: re-calls the SAME endpoint for the SAME policy. The backend re-checks the stored
  // transaction and never sends a second payment; this never re-runs the create-policy / pay flow from scratch.
  async function checkAgain() {
    if (!policy || !pending || busy.current) return;
    busy.current = true; setRunning(true); setError(''); setElapsed(0); setPhase('Checking whether the payment landed - nothing is sent again');
    try {
      if (pendingAction.kind === 'resolve') { const res = await resolvePolicy(policy.id, pendingAction.release); if (isPending(res)) setPending(res); else { setPending(null); applyResolution(res); } }
      else { const out = await evaluatePolicy(policy.id, evalOptions()); if (isPending(out)) setPending(out); else { setPending(null); setResult(out); } }
    }
    catch (e) { setError(message(e)); }
    finally { busy.current = false; setRunning(false); setPhase(''); void refresh(); }
  }
  const escrow = result?.escrow ?? null;
  const escrowState: 'none' | 'pending' | 'released' | 'voided' = escrow ? escrow.status : 'none';
  const releasedAmount = escrow?.released_amount_sol ?? escrow?.amount_sol ?? 0;
  const satReading = result?.readings.find(r => r.unit === 'ndvi') ?? null;
  const satImage = satReading?.image_url ? `${apiUrl(satReading.image_url)}&v=${encodeURIComponent(result?.policy_id ?? '')}` : null;
  if (page !== 'playground' && page !== 'activity') return <Landing/>;
  return <div className="app-shell" data-theme={page === 'playground' ? theme : undefined}>
    <header className={`topbar${scrolled ? ' scrolled' : ''}`}>
      <span className="corner corner-left"><MapPin size={14}/> {form.region.trim() || 'Warsaw, PL'}</span>
      <nav className="pill" aria-label="Main navigation"><a className="pill-brand" href="#overview" aria-label="Agent Pay overview">Agent Pay</a>{["overview","playground","activity"].map(item => <a key={item} href={"#"+item} aria-current={page === item ? "page" : undefined}>{item}</a>)}<a className="pill-cta" href="#overview"><Zap size={14}/> Overview</a></nav>
      <span className="corner corner-right"><span className="solana-mark" aria-hidden="true">≋</span> {balance ? balance.balance_sol.toFixed(3)+" SOL" : "devnet"}</span>
    </header>
    <main id="workspace">
      {page === 'playground' && (() => {
        const T = Number.isFinite(trigger) && trigger > 0 ? trigger : 40, X = Number.isFinite(exit) && exit >= 0 ? exit : 10, S = Number.isFinite(sum) && sum > 0 ? sum : 0.01;
        const dry = cover === 'drought';
        const isCrop = preset === CUSTOM || !!currentProduct?.product_type.startsWith('crop_');
        const table = [0, 0.25, 0.5, 0.75, 1].map(r => ({ r, mm: T - r * (T - X), pay: S * r }));
        const eventStatusRatio = simEventStatus === 'cancelled' || simEventStatus === 'postponed' ? 1 : simEventStatus === 'onsale' ? 0 : null;
        const simRatios = mode === 'simulate' ? [num(simA), num(simB)].filter(Number.isFinite).map(mm => ratioOf(mm, T, X)).concat(simNdvi.trim() !== '' && Number.isFinite(num(simNdvi)) ? [ratioOf(num(simNdvi), ndviT, ndviE)] : []).concat(currentProduct?.venue_lookup && eventStatusRatio !== null ? [eventStatusRatio] : []) : [];
        const pf = simRatios.length ? Math.min(...simRatios) : 0, pc = simRatios.length ? Math.max(...simRatios) : 0, pDisagree = simRatios.length > 1 && pc - pf > 0.10;
        const steps = ['Your field', 'Your rule', 'The weather', 'Your money'];
        // "Just show me a payout": the selected product's own defaults, both sources agreeing near the middle of its rule, straight to Step 4.
        const quickDemo = () => { if (running || pending || !canNext || !directionOk) return; const t = num(form.trigger_mm), x = num(form.exit_mm); const a = +(t + 0.48 * (x - t)).toFixed(1), b = +(t + 0.52 * (x - t)).toFixed(1); setMode('simulate'); setSimA(String(a)); setSimB(String(b)); setSimNdvi(''); setSatellite(false); setStep(4); window.scrollTo({ top: 0, behavior: 'smooth' }); void run({ a, b }); };
        const canNext = step === 1 ? form.region.trim() !== '' && Number.isFinite(num(form.lat)) && Number.isFinite(num(form.lon)) && S > 0 : step === 2 ? directionOk && ndviE < ndviT : true;
        return <section className="playground guide">
          <div className="guide-head"><span className="eyebrow">PLAYGROUND</span><h1>Insure a field, step by step.</h1></div>
          <ol className="guide-steps" aria-label="Progress">{steps.map((s, i) => <li key={s} className={step === i + 1 ? 'now' : step > i + 1 ? 'done' : ''}><span>{step > i + 1 ? <Check size={13}/> : i + 1}</span>{s}</li>)}</ol>

          <div className="guide-card"><div className="guide-pane" key={`pane-${step}-${result ? result.dispute_status : "draft"}`}>
            {step === 1 && <div className="guide-body">
              <h2>Step 1 · What are we covering, and for how much?</h2>
              <p className="guide-what">What we protect, and the most it can ever receive. Nothing is paid yet.</p>
              <div className="guide-grid">
                <div className="field"><span>What are you covering? <Hint text="Each product comes with a ready-made rule that you can adjust in the next step. Pick 'My own rule' to also choose the direction and the satellite thresholds."/></span><div className="crop-pick five" role="radiogroup" aria-label="Product">{catalogError ? <span className="pick-note" role="alert">Couldn't load the product list. {catalogError}</span> : !catalog.length ? <span className="pick-note">Loading products…</span> : [...catalog.map(p => ({ key: p.product_type as string, label: p.label, hint: p.icon_hint })), { key: CUSTOM, label: 'My own rule', hint: 'sliders' }].map(o => { const Icon = iconFor[o.hint] ?? Package; return <button key={o.key} type="button" role="radio" aria-checked={preset === o.key} disabled={running} onClick={() => choosePreset(o.key)}><em><Icon size={20} aria-hidden="true"/></em>{o.label}</button>; })}</div></div>
                <label className="field"><span>{currentProduct?.place_prompt ?? 'Where is this?'} <Hint text="A place name for you. The weather and the satellite are read for the exact spot below, not for the name."/></span><input value={form.region} disabled={running} onChange={set('region')} placeholder="Warsaw, PL"/></label>
                {currentProduct?.venue_lookup && <label className="field"><span>Venue name (optional) <Hint text="If the venue is on Ticketmaster, its event status (cancelled, postponed) is read as a second, non-weather source. Leave empty to use weather only."/></span><input value={form.venue_name} disabled={running} onChange={set('venue_name')} placeholder="e.g. Tauron Arena Kraków"/></label>}
                {currentProduct?.venue_lookup && <label className="field"><span>Event date <Hint text="The day of the concert or festival. Weather is read for this day, and (with a venue name) Ticketmaster is checked for a real event at that venue on this date. Leave empty to default to the coming week."/></span><input type="date" value={form.event_date} disabled={running} onChange={set('event_date')}/></label>}
                <label className="field"><span>Latitude <Hint text={`The exact spot of your ${place}, north to south. Right-click it in Google Maps and copy the first number.`}/></span><input inputMode="decimal" value={form.lat} disabled={running} onChange={set('lat')}/></label>
                <label className="field"><span>Longitude <Hint text={`The exact spot of your ${place}, east to west. The second number from Google Maps.`}/></span><input inputMode="decimal" value={form.lon} disabled={running} onChange={set('lon')}/></label>
                <label className="field"><span>Cover amount (SOL) <Hint text={`The most you can receive ${M.period} if your rule is hit at its worst. SOL is the money on the Solana network; here it is test money on devnet.`}/></span><input inputMode="decimal" value={form.sum_insured_sol} disabled={running} onChange={set('sum_insured_sol')}/></label>
                <label className="field"><span>{payeeLabel} (optional) <Hint text="The Solana address the payout goes to. Leave it empty and the demo wallet receives it."/></span><input value={form.payee_pubkey} disabled={running} onChange={set('payee_pubkey')} placeholder="the demo wallet"/></label>
              </div>
              <div className="guide-meaning"><b>What this means</b><p>You are insuring <em>{currentProduct?.subject ?? 'a custom policy'}</em> {isDemo ? 'from' : 'near'} <em>{form.region.trim() || '…'}</em>. The most it can receive is <em>{sol4(S)}</em>. That money leaves the insurer's wallet on Solana only if the rule in the next step is hit.{isDemo && ` ${currentProduct?.source_note}`}</p></div>
            </div>}

            {step === 2 && <div className="guide-body">
              <h2>Step 2 · The rule that pays</h2>
              <p className="guide-what">One line decides everything. No adjuster, no phone call, no one can change the number afterwards.</p>
              <div className="guide-grid">
                {preset === CUSTOM && <label className="field"><span>Pay when rain is <Hint text="Drought: the less it rains, the more you get. Flooding: the more it rains, the more you get."/></span><select className="gsel" value={cover} disabled={running} onChange={e => chooseCover(e.target.value as Cover)}><option value="drought">too little (drought)</option><option value="excess_rain">too much (flooding)</option></select></label>}
                <label className="field"><span>Starts paying at ({M.unit}) <Hint text={`Also called the trigger. The ${M.noun} ${M.period}: at this value you get nothing yet; past it the payout begins to grow.`}/></span><input inputMode="decimal" value={form.trigger_mm} disabled={running} onChange={set('trigger_mm')}/></label>
                <label className="field"><span>Pays in full at ({M.unit}) <Hint text={`Also called the exit. At this ${M.noun} you get the whole cover amount.`}/></span><input inputMode="decimal" value={form.exit_mm} disabled={running} onChange={set('exit_mm')}/></label>
                {preset === CUSTOM && <details className="src-more" style={{ gridColumn: '1 / -1' }}><summary>Satellite settings (optional)</summary><div className="guide-grid" style={{ marginTop: 12 }}>
                  <label className="field"><span>Healthy field score <Hint text="Also called healthy NDVI. NDVI is a 0 to 1 greenness score from satellite photos. Around 0.6 and above means a healthy green field. At this value the satellite says no loss."/></span><input inputMode="decimal" value={form.ndvi_trigger} disabled={running} onChange={set('ndvi_trigger')}/></label>
                  <label className="field"><span>Dead field score <Hint text="Also called total-loss NDVI. Below about 0.3 the field looks bare or dead from space. At this value the satellite says total loss."/></span><input inputMode="decimal" value={form.ndvi_exit} disabled={running} onChange={set('ndvi_exit')}/></label>
                </div></details>}
              </div>
              <p className="guide-rule">Your rule is <em>{(currentProduct?.label ?? 'custom cover').toLowerCase()}</em>: payout starts once the {M.noun} {dry ? 'falls below' : 'passes'} <em>{T} {M.unit}</em>, and it is paid in full at <em>{X} {M.unit}</em>.</p>
              <details className="src-more"><summary>See the exact numbers</summary><div className="guide-meaning" style={{ marginTop: 10 }}><b>What you would earn</b>
                <table className="earn"><thead><tr><th>If the {M.noun} {M.period} is</th><th>You get</th></tr></thead><tbody>{table.map(row => <tr key={row.r}><td>{row.r === 0 ? `${row.mm} ${M.unit} or ${dry ? 'more' : 'less'}` : row.r === 1 ? `${row.mm} ${M.unit} or ${dry ? 'less' : 'more'}` : `about ${row.mm.toFixed(0)} ${M.unit}`}</td><td><b>{sol4(row.pay)}</b> <span>{pct(row.r)} of the cover</span></td></tr>)}</tbody></table>
                <p>In between it is a straight line. {dry ? `Less ${M.noun}, more money.` : `More ${M.noun}, more money.`}</p></div></details>
            </div>}

            {step === 3 && <div className="guide-body">
              <h2>Step 3 · {M.who}</h2>
              <p className="guide-what">{isDemo ? `We never trust one source. Two independent readings. If they disagree, only the part they both agree on is paid at once, and the rest is held. ${currentProduct?.source_note}` : 'We never trust one source. Two independent weather models' + (isCrop ? ', and the satellite if you like' : '') + '. If they disagree, only the part they all agree on is paid at once, and the rest is held.'}</p>
              <div className="guide-options" role="radiogroup" aria-label="Readings source">{activePresets.map(p => { const active = mode === p.mode && (p.mode === 'live' || (simA === p.a && simB === p.b && simNdvi === (p.ndvi ?? ''))); return <button key={p.label} type="button" role="radio" aria-checked={active} disabled={running} onClick={() => { setMode(p.mode); if (p.a && p.b) { setSimA(p.a); setSimB(p.b); } setSimNdvi(p.ndvi ?? ''); }}><i/><span><b>{scenarioCopy[p.label].title} <em>{p.mode === 'live' ? 'real data' : 'demo'}</em></b><small>{scenarioCopy[p.label].text}</small></span></button>; })}</div>
              {mode === 'live' ? (isCrop && <label className="wiz-check"><input type="checkbox" checked={satellite} disabled={running} onChange={e => setSatellite(e.target.checked)}/><span>Also look at the field from space (satellite crop health)</span></label>)
                : <div className="guide-grid three"><label className="field"><span>{isDemo ? 'Feed' : 'Model'} A says ({M.unit}) <Hint text="Demo only: pretend this is what the first weather model reported for the week."/></span><input inputMode="decimal" value={simA} disabled={running} onChange={e => setSimA(e.target.value)}/></label><label className="field"><span>{isDemo ? 'Feed' : 'Model'} B says ({M.unit}) <Hint text="Demo only: pretend this is what the second, independent weather model reported."/></span><input inputMode="decimal" value={simB} disabled={running} onChange={e => setSimB(e.target.value)}/></label>{isCrop && <label className="field"><span>Field greenness from space (optional) <Hint text="Also called NDVI. Demo only: pretend greenness score of the field from space. 0.20 looks dead, 0.65 looks healthy. Leave empty to skip the satellite."/></span><input inputMode="decimal" value={simNdvi} disabled={running} onChange={e => setSimNdvi(e.target.value)} placeholder="leave empty for none"/></label>}{currentProduct?.venue_lookup && <label className="field"><span>Event status (optional) <Hint text="Demo only: pretend Ticketmaster reports this status for the event, as a second, non-weather source. Leave on 'skip' to use weather only."/></span><select className="gsel" value={simEventStatus} disabled={running} onChange={e => setSimEventStatus(e.target.value)}><option value="">skip (weather only)</option><option value="onsale">still on sale</option><option value="postponed">postponed</option><option value="cancelled">cancelled</option></select></label>}</div>}
              <div className="guide-meaning"><b>What will happen</b>
                {mode === 'live' ? <>
                  <p>We read the real {M.noun} {M.period}{satellite ? ', and a satellite photo of your field' : ''}. Whatever it was, the rule pays for it.</p>
                  <div className="src-strip"><span><i/>Open-Meteo</span><span><i/>ECMWF</span>{satellite && <span><i/>Sentinel-2 / Landsat</span>}{currentProduct?.venue_lookup && <span><i/>Ticketmaster</span>}</div>
                  {currentProduct?.venue_lookup && <p>{form.venue_name.trim() ? `We also check whether "${form.venue_name.trim()}" is on sale, postponed or cancelled on Ticketmaster — an independent, non-weather signal.` : 'Add a venue name in Step 1 to also check the event\'s real status on Ticketmaster (independent of the weather).'}</p>}
                  <details className="src-more"><summary>Where do these come from?</summary>
                    <ul className="pre-sources pre-trust">
                      <li><b>Weather model A</b><span><strong>Open-Meteo, "best match"</strong> · a Swiss non-profit that blends the national weather services and picks the best one for your spot. <a href="https://open-meteo.com/en/docs" target="_blank" rel="noreferrer">See the source <ArrowUpRight size={12}/></a></span></li>
                      <li><b>Weather model B</b><span><strong>ECMWF IFS</strong> · the European forecast centre, funded by 35 states. Built separately from A, so it fails differently. <a href="https://www.ecmwf.int/en/forecasts" target="_blank" rel="noreferrer">See the source <ArrowUpRight size={12}/></a></span></li>
                      <li className="pre-trust-note"><b>One clarification</b><span>Both readings are fetched through Open-Meteo's API — it's one provider re-serving two different underlying forecast models, not two separate companies queried independently.</span></li>
                      {satellite && <li><b>Satellite</b><span><strong>Sentinel-2 and Landsat 8</strong> · photos from ESA and NASA, read as a 0 to 1 greenness score of your field. <a href="https://agromonitoring.com/" target="_blank" rel="noreferrer">See the source <ArrowUpRight size={12}/></a></span></li>}
                      {currentProduct?.venue_lookup && <li><b>Event status</b><span><strong>Ticketmaster Discovery API</strong> · whether the event is on sale, postponed or cancelled, from the venue's own ticketing system. Only checked when a venue name was given in Step 1, and only if a matching event is found. <a href="https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/" target="_blank" rel="noreferrer">See the source <ArrowUpRight size={12}/></a></span></li>}
                      <li className="pre-trust-note"><b>Why you can trust it</b><span>Nobody types these numbers. They are fetched live when you press the button, every reading is shown afterwards, and the payment is on Solana for anyone to check.</span></li>
                    </ul>
                  </details>
                </> : (() => {
                  const rows: { name: string; v: number; unit: 'metric' | 'ndvi' | 'status' }[] = [{ name: isDemo ? `${M.label} feed A` : 'Weather model A', v: num(simA), unit: 'metric' }, { name: isDemo ? `${M.label} feed B` : 'Weather model B', v: num(simB), unit: 'metric' }]; if (simNdvi.trim() !== '' && Number.isFinite(num(simNdvi))) rows.push({ name: 'Satellite', v: num(simNdvi), unit: 'ndvi' }); if (currentProduct?.venue_lookup && eventStatusRatio !== null) rows.push({ name: 'Event status', v: eventStatusRatio, unit: 'status' }); const usable = rows.filter(r => Number.isFinite(r.v));
                  const withR = usable.map(r => ({ ...r, r: r.unit === 'ndvi' ? ratioOf(r.v, ndviT, ndviE) : r.unit === 'status' ? r.v : ratioOf(r.v, T, X) }));
                  const lo = withR.reduce((m, r) => r.r < m.r ? r : m, withR[0]), hi = withR.reduce((m, r) => r.r > m.r ? r : m, withR[0]);
                  return <>
                    {pDisagree ? <>
                      <p><em>{sol4(S * pf)}</em> is paid at once. <em>{sol4(S * (pc - pf))}</em> waits, because <em>{lo.name}</em> and <em>{hi.name}</em> don't agree.</p>
                    </> : <>
                      <p>{pf > 0.01 ? <>The sources agree. <em>{sol4(S * pf)}</em> is paid at once, nothing waits.</> : <>The sources agree: the crop is fine, nothing is owed.</>}</p>
                    </>}
                    <details className="src-more"><summary>What each source says</summary><ul className="pre-sources">{withR.map(r => <li key={r.name}><b>{r.name}</b><span>says <em>{r.unit === 'ndvi' ? `NDVI ${r.v.toFixed(2)}` : r.unit === 'status' ? (r.v >= 1 ? 'cancelled / postponed' : 'still on sale') : `${r.v.toFixed(0)} ${M.unit} of ${M.noun}`}</em>{r.unit === 'ndvi' ? (r.r >= 0.5 ? ', the field looks stressed from space' : ', the field looks healthy from space') : r.unit === 'status' ? '' : (dry ? (r.v >= T ? ', enough rain, no loss' : r.v <= X ? ', very dry, total loss' : ', a dry week, some loss') : (r.v <= T ? ', normal rain, no loss' : r.v >= X ? ', flooded, total loss' : ', a wet week, some loss'))}. By your rule that is <em>{pct(r.r)}</em> of the cover, <em>{sol4(S * r.r)}</em>.</span></li>)}</ul></details>
                  </>;
                })()}
              </div>
            </div>}

            {step === 4 && <div className="guide-body">
              <h2>Step 4 · Your money</h2>
              {pending && <div className="pending-card" role="status" aria-live="polite">
                <b><Hourglass size={18}/> Still confirming</b>
                <p>The payment was sent, but the network has not confirmed it yet. It may already have gone through, so <strong>do not pay again</strong>. Check again in a moment.</p>
                <div className="pending-meta"><span>{pending.floor_amount_sol != null ? `${sol4(pending.floor_amount_sol)} · ` : ''}transaction <code title={pending.tx_signature}>{pending.tx_signature.slice(0, 6)}…{pending.tx_signature.slice(-6)}</code></span>{pending.floor_explorer_url && <a className="external" href={pending.floor_explorer_url} target="_blank" rel="noreferrer">See it on Solana <ArrowUpRight size={13}/></a>}</div>
                <button type="button" className="primary" disabled={running} onClick={() => void checkAgain()}>{running ? <><RefreshCw size={15} className="spin"/> {phase || 'Checking'}…</> : <><RefreshCw size={15}/> Check again</>}</button>
              </div>}
              {!result && !pending && !running && !error && <p className="guide-what">Everything is set. Press the button and watch the money move on Solana devnet.</p>}
              {!result && <div className="guide-summary"><span>{currentProduct?.label ?? 'Custom rule'} · {form.region.trim() || '…'}</span><span>Cover {sol4(S)}</span><span>{M.label} rule {T} → {X} {M.unit}</span><span>{mode === 'live' ? 'Real weather' : 'Demo readings'}</span></div>}
              {!result && !pending && <div className="wiz-go"><button className="primary wiz-btn" disabled={running || !formValid} type="button" onClick={() => void run()}>{running ? <><RefreshCw size={16} className="spin"/> {phase || 'Working'}…</> : <>Check the {M.noun} and pay me <ArrowUpRight size={18}/></>}</button><span>Real devnet SOL. It takes a few seconds.</span></div>}
              {running && <div className="working"><span className="pulse"/><span>{phase || 'Working'}…</span><span>{elapsed.toFixed(0)}s</span></div>}
              {error && <div className="error-banner" role="alert"><AlertCircle size={18}/><span>{error}</span></div>}
              {result && (() => {
                const weather = result.readings.filter(r => r.unit !== 'ndvi' && r.unit !== 'status'), evStatus = result.readings.find(r => r.unit === 'status'), sat = result.readings.find(r => r.unit === 'ndvi');
                const agree = result.dispute_status === 'none';
                const paid = result.floor_amount_sol, held = escrowState === 'none' ? 0 : escrow!.amount_sol;
                const got = paid + (escrowState === 'released' ? releasedAmount : 0);
                const plainWhy = agree ? '' : sat && weather.length && Math.abs((sat.payout_ratio ?? 0) - (weather.reduce((s, r) => s + (r.payout_ratio ?? 0), 0) / weather.length)) > 0.1
                  ? `The weather reports say one thing, the satellite picture of your field says another. We paid what all of them agree on. The rest waits until someone checks the picture.`
                  : `The two ${M.sources} don't match (${weather.map(r => `${r.observed_mm.toFixed(0)} ${r.unit}`).join(' vs ')}). We paid what both agree on. The rest waits for a fresh reading or a person.`;
                return <div className="flow" key={`${result.policy_id}-${result.cycle}-${result.dispute_status}`}>
                  <div className="flow-verdict">{got > 0 ? <><span className="flow-big flow-win"><ArrowUpRight size={28}/></span><div><b>You got paid.</b><p>{sol4(got)} is in your wallet{held > 0 && escrowState === 'pending' ? `, and ${sol4(held)} more may follow.` : '.'}</p></div></> : held > 0 && escrowState === 'pending' ? <><span className="flow-big flow-wait"><Hourglass size={26}/></span><div><b>Not yet.</b><p>{sol4(held)} is waiting for a decision.</p></div></> : <><span className="flow-big flow-none"><Check size={26}/></span><div><b>No payout.</b><p>Your rule was not hit, so nothing is owed.</p></div></>}</div>
                  <ol className="flow-chain">
                    <li className="flow-box"><small>1 · {M.label} {M.period}</small><b>{weather.map(r => `${r.observed_mm.toFixed(0)} ${r.unit}`).join(' · ')}</b><span>{weather.length > 1 ? (agree ? 'both reports match' : 'reports differ') : 'one report'}{evStatus ? ` · event status: ${evStatus.detail.match(/status\.code=(\w+)/)?.[1] ?? 'known'}` : ''}{sat ? ` · field from space: ${(sat.payout_ratio ?? 0) >= 0.5 ? 'looks bad' : 'looks fine'}` : ''}</span></li>
                    <li className="flow-arrow" aria-hidden="true"><ArrowRight size={22}/><ArrowDown size={22}/></li>
                    <li className={`flow-box ${paid > 0 ? 'flow-box-win' : ''}`}><small>2 · Paid to you now</small><b>{sol4(paid)}</b><span>{paid > 0 ? 'already in your wallet' : 'nothing owed'}</span>{result.floor_explorer_url && <a className="external" href={result.floor_explorer_url} target="_blank" rel="noreferrer">See it on Solana <ArrowUpRight size={13}/></a>}</li>
                    <li className="flow-arrow" aria-hidden="true"><ArrowRight size={22}/><ArrowDown size={22}/></li>
                    <li className={`flow-box ${escrowState === 'pending' ? 'flow-box-wait' : escrowState === 'released' ? 'flow-box-win' : escrowState === 'voided' ? 'flow-box-lost' : ''}`}><small>3 · {escrowState === 'none' ? 'Waiting' : escrowState === 'pending' ? 'Still waiting' : escrowState === 'released' ? 'Was waiting, now paid' : 'Was waiting, not paid'}</small><b>{escrowState === 'none' ? 'nothing' : escrowState === 'released' ? sol4(releasedAmount) : sol4(held)}</b><span>{escrowState === 'none' ? 'everyone agreed' : escrowState === 'pending' ? (result.dispute_status === 'escalated' ? 'a person must decide' : 'settles at the next reading') : escrowState === 'released' ? 'sent to your wallet' : 'the higher reading was not trusted'}</span>{(escrow?.escrow_pda ?? result.escrow_pda) && <span className="pda-note">Locked in a program-controlled account — not held by us.<a className="external" href={escrow?.escrow_explorer_link ?? result.escrow_explorer_link ?? '#'} target="_blank" rel="noreferrer" title={escrow?.escrow_pda ?? result.escrow_pda ?? ''}>{(escrow?.escrow_pda ?? result.escrow_pda ?? '').slice(0, 4)}…{(escrow?.escrow_pda ?? result.escrow_pda ?? '').slice(-4)} on Explorer <ArrowUpRight size={13}/></a></span>}{escrow?.release_explorer_url && <a className="external" href={escrow.release_explorer_url} target="_blank" rel="noreferrer">See it on Solana <ArrowUpRight size={13}/></a>}</li>
                  </ol>
                  {escrowState === 'pending' && <div className="flow-decide"><p><b>What should happen to the {sol4(held)} that is waiting?</b></p><div className="flow-buttons"><button type="button" disabled={running || !!pending} onClick={() => void resolve(true)}><ArrowUpRight size={16}/> Pay it to me</button><button type="button" className="flow-no" disabled={running || !!pending} onClick={() => void resolve(false)}><XIcon size={16}/> Don't pay it</button>{result.dispute_status === 'investigating' && <button type="button" className="flow-again" disabled={running || !!pending} onClick={() => void nextCycle()}><RefreshCw size={15}/> Check the weather again</button>}</div></div>}
                  {!agree && <div className="flow-why"><b>Why part of it waits <span className="ai-badge">{providerBadge(result.dispute)}</span></b><p>{plainWhy}</p><details><summary>The full explanation</summary><p>{result.dispute?.summary}</p><small>{result.dispute?.ai_used ? `Written by ${result.dispute.model}` : 'Rule-based check'}. It can explain and flag. It can never pay.</small></details></div>}
                  <div className="flow-total"><span>Your cover was <b>{policy ? sol4(policy.sum_insured_sol) : '—'}</b></span><span>Received <b>{sol4(got)}</b></span>{escrowState === 'pending' && <span>Still possible <b>{sol4(held)}</b></span>}</div>
                  {result.proof && <details className="pg-details proof"><summary>Show the math</summary>
                    <div className="proof-body">
                      <p className="proof-ai"><b>{result.proof.ai_involvement === 'none' ? 'No AI was involved. The formula alone set this amount.' : 'The AI did not decide this amount. It only wrote the explanation of the disagreement.'}</b></p>
                      <table className="proof-table"><thead><tr><th>Source</th><th>Reading</th><th>Fetched</th><th>Payout ratio</th></tr></thead><tbody>{result.proof.readings.map(r => <tr key={r.source}><td>{r.source} <span className={r.live ? 'tag-live' : 'tag-sim'}>{r.live ? 'live' : 'demo'}</span></td><td>{r.unit === 'status' ? `event status: ${r.value}` : `${r.value} ${r.unit}`}</td><td>{r.fetched_at.replace('T', ' ').replace('Z', ' UTC')}</td><td>{r.payout_ratio.toFixed(4)}</td></tr>)}</tbody></table>
                      <dl className="proof-lines"><div><dt>Trigger / exit</dt><dd>{result.proof.trigger} {result.proof.metric_unit} / {result.proof.exit} {result.proof.metric_unit}</dd></div><div><dt>Formula</dt><dd><code>{result.proof.formula}</code></dd></div><div><dt>Floor (paid now)</dt><dd><code>{result.proof.floor_calc}</code></dd></div><div><dt>Ceiling (most possible)</dt><dd><code>{result.proof.ceiling_calc}</code></dd></div><div><dt>Disagreement</dt><dd>spread {pct(result.spread)} vs tolerance {pct(result.proof.tolerance)}</dd></div><div><dt>Inputs hash</dt><dd><code className="proof-hash">sha256 {result.proof.inputs_hash}</code></dd></div>{result.proof.data_source_note && <div><dt>Data source</dt><dd>{result.proof.data_source_note}</dd></div>}</dl>
                    </div>
                  </details>}
                <details className="pg-details numbers"><summary>All the numbers, explained</summary>
                  <div className="num-block"><h4>Where each number came from</h4>
                    <div className="num-sources">{result.readings.map(r => { const ratio = r.payout_ratio ?? 0; const who = r.unit === 'status' ? { name: r.live ? 'Ticketmaster event status' : 'Ticketmaster event status (demo value)', what: r.live ? 'Whether the event is on sale, postponed or cancelled, read from the venue\'s own ticketing system.' : 'A pretend event status, chosen for the demo.' } : r.source.includes('best_match') ? { name: 'Open-Meteo, best match', what: 'A blend of national weather services. Weather model A.' } : r.source.includes('ecmwf') ? { name: 'ECMWF forecast model', what: 'The European centre\'s global model. Weather model B, independent of A.' } : r.source.includes('agromonitoring') ? { name: 'Satellite (Agromonitoring)', what: 'Greenness of the field from Sentinel-2 and Landsat photos.' } : r.unit === 'ndvi' ? { name: 'Satellite (demo value)', what: 'A pretend greenness score, typed in for the demo.' } : { name: isDemo ? `${M.label} feed ${r.source.endsWith('B') ? 'B' : 'A'} (demo value)` : `Weather model ${r.source.split(':')[1] ?? ''} (demo value)`, what: `A pretend ${M.noun} value, typed in for the demo.` };
                      return <div key={r.source} className="num-source"><div className="num-source-head"><b>{who.name}</b><span className={r.live ? 'tag-live' : 'tag-sim'}>{r.live ? 'real data' : 'demo'}</span></div><p>{who.what}</p><div className="num-source-row"><span>It reported</span><b>{r.unit === 'ndvi' ? `NDVI ${r.observed_mm.toFixed(2)}` : r.unit === 'status' ? (r.detail.match(/status\.code=(\w+)/)?.[1] ?? (r.observed_mm >= 1 ? 'cancelled / postponed' : 'on sale')) : `${r.observed_mm.toFixed(1)} ${M.unit} of ${M.noun}`}</b></div><div className="num-source-row"><span>By your rule that means</span><b className={ratio >= 0.5 ? 'pg-bad' : ratio > 0.01 ? 'pg-mid' : 'pg-ok'}>{ratio <= 0.01 ? 'no loss, 0% payout' : ratio >= 0.99 ? 'total loss, 100% payout' : `${pct(ratio)} of the cover`}</b></div>{r.detail && r.live && <small>{r.detail}</small>}</div>; })}</div>
                  </div>
                  <div className="num-block"><h4>How the money was worked out</h4>
                    <ol className="num-math">
                      <li><span>Lowest payout any source allows</span><b>{pct(result.payout_ratio_floor)}</b><small>Everyone agrees on at least this much, so it was paid right away: {pct(result.payout_ratio_floor)} of {policy ? sol4(policy.sum_insured_sol) : '—'} = <em>{sol4(result.floor_amount_sol)}</em>.</small></li>
                      <li><span>Highest payout any source allows</span><b>{pct(result.payout_ratio_ceiling)}</b><small>{result.spread > result.tolerance ? <>The gap between lowest and highest is {pct(result.spread)}. Anything over {pct(result.tolerance)} counts as a disagreement, so the difference was held: <em>{sol4(result.escrow_amount_sol)}</em>.</> : <>The gap is only {pct(result.spread)}, within the {pct(result.tolerance)} allowance, so the sources count as agreeing and nothing was held.</>}</small></li>
                      <li><span>The rule itself</span><b>{policy?.cover === 'excess_rain' ? 'more rain, more payout' : 'less rain, more payout'}</b><small>payout = (trigger − rain) ÷ (trigger − exit), kept between 0 and 1. With trigger {policy?.trigger_mm} mm and exit {policy?.exit_mm} mm.{satReading ? ` The satellite uses the same shape with NDVI ${policy?.ndvi_trigger} (healthy) and ${policy?.ndvi_exit} (total loss).` : ''}</small></li>
                    </ol>
                  </div>
                  {satReading && <div className="num-block"><h4>The field from space</h4><figure className="satellite-proof"><div className="satellite-frame">{satImage ? <img src={satImage} alt={`NDVI image of the field, ${satReading.live ? 'satellite scene' : 'simulated'}`}/> : <div className="satellite-missing"><Satellite size={22}/><span>No scene available</span></div>}</div><figcaption><strong><Satellite size={13}/> Greenness map {satReading.live ? <span className="tag-live">real scene</span> : <span className="tag-sim">demo</span>}</strong><span className="satellite-stat"><em>{satReading.observed_mm.toFixed(2)}</em> average greenness (NDVI)</span>{satReading.captured_at && <span>photographed {satReading.captured_at}</span>}<span>Green means healthy plants, brown means bare soil or dead crop.</span></figcaption></figure></div>}
                  {result.dispute && <div className="num-block"><h4>What the watchdog checked</h4><ul className="num-evidence">{result.dispute.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul><small className="num-foot">{result.dispute.ai_used ? `Written by ${result.dispute.model}` : 'Rule-based check'}. It can explain and flag. It can never move money.</small></div>}
                  <div className="num-block num-policy"><h4>This policy</h4><dl><div><dt>Policy number</dt><dd>{result.policy_id}</dd></div><div><dt>Field</dt><dd>{policy?.region} · {policy?.lat}, {policy?.lon}</dd></div><div><dt>Week covered</dt><dd>{policy?.window_start} to {policy?.window_end}</dd></div><div><dt>Cover</dt><dd>{policy ? sol4(policy.sum_insured_sol) : '—'}</dd></div><div><dt>Paid to</dt><dd className="num-mono">{policy?.payee_pubkey ? `${policy.payee_pubkey.slice(0, 6)}…${policy.payee_pubkey.slice(-6)}` : '—'}</dd></div><div><dt>Time to settle</dt><dd>{(result.elapsed_ms / 1000).toFixed(2)} seconds</dd></div></dl>{result.note && <p className="payment-note">{result.note}</p>}</div>
                </details>
                </div>;
              })()}
            </div>}

            </div>
            <div className="guide-nav">
              {step > 1 && !running && !result && !pending ? <button type="button" className="guide-back" onClick={() => { if (result) { setResult(null); setPolicy(null); setError(''); setStep(1); } else setStep(step - 1); window.scrollTo({ top: 0, behavior: 'smooth' }); }}>{result ? 'Start over' : 'Back'}</button> : step === 1 && !running && !pending && !result ? <button type="button" className="guide-back guide-demo" disabled={!canNext || !directionOk} onClick={quickDemo} title="Skip the steps: fills in sensible defaults and shows a payout">Just show me a payout</button> : <span/>}
              {step < 4 && <button type="button" className="primary guide-next" disabled={!canNext} onClick={() => setStep(step + 1)}>Next: {steps[step]} <ArrowUpRight size={16}/></button>}
              {step === 4 && result && !running && !pending && <button type="button" className="primary guide-next" onClick={() => { setResult(null); setPolicy(null); setError(''); setStep(1); window.scrollTo({ top: 0, behavior: 'smooth' }); }}>Insure another field <ArrowUpRight size={16}/></button>}
            </div>
          </div>
        </section>;
      })()}
      {page === 'activity' && <section className="activity"><div className="page-heading"><div><span className="eyebrow">ON-CHAIN RECORDS</span><h1>Wallet <em>activity.</em></h1><p>Inspect the payouts sent by the insurer wallet.</p></div><a className="secondary-link" href="#playground">New policy <ArrowUpRight size={16}/></a></div><details className="wallet" open>
        <summary><span><Wallet size={17}/> Insurer wallet</span><span>{balance ? balance.balance_sol.toLocaleString('en-US',{maximumFractionDigits:6}) : refreshing ? 'Loading…' : 'Unavailable'} {balance && 'SOL'}<ChevronRight size={16}/></span></summary>
        <div className="wallet-content"><div className="wallet-address"><span>{cluster}</span>{balance && <div><a href={explorer('address',balance.address)} target="_blank" rel="noreferrer">{short(balance.address)} <ArrowUpRight size={14}/></a><button aria-label={copied ? 'Address copied' : 'Copy wallet address'} onClick={async () => { try { await navigator.clipboard.writeText(balance.address); setCopied(true); } catch { setBalanceError('Clipboard unavailable. Open the wallet link to copy the address.'); } }}>{copied ? <Check size={15}/> : <Copy size={15}/>}</button></div>}</div>
        <div className="history-heading"><h2>Recent transactions</h2><button className="refresh" disabled={refreshing} onClick={() => void refresh()}><RefreshCw size={14} className={refreshing ? 'spin' : ''}/> Refresh</button></div>
        {balanceError && <p className="inline-error" role="alert">{balanceError}{balance && ' Showing last known balance.'}</p>}
        {historyError && <p className="inline-error" role="alert">{historyError}{history && ' Showing last retrieved transactions.'}</p>}
        {history?.transactions.length ? <ul className="transaction-list">{history.transactions.map(tx => <li key={tx.signature}><a href={explorer('tx',tx.signature)} target="_blank" rel="noreferrer" aria-label={'View transaction '+tx.signature}><span>{short(tx.signature)}</span><span className={tx.err == null ? 'success' : 'failed'}>{tx.err == null ? 'Successful' : 'Failed'} <ArrowUpRight size={14}/></span></a></li>)}</ul> : <p className="empty">{refreshing ? 'Loading transactions…' : historyError ? 'History is unavailable right now.' : 'No transactions yet.'}</p>}
        </div>
      </details></section>}
      {balanceError && <p className="wallet-warning">Wallet could not refresh{balance ? ' · Balance may be out of date' : ''}. Visit Activity to inspect wallet details.</p>}
      <footer><span className="footer-wordmark">agentpay</span><span>Parametric cover. Deterministic payouts.</span><span className="footer-end">SOLANA / AGENT PAY DEMO</span></footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
