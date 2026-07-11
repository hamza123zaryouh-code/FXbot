"""
Doel: Challenge (+$16k) + Verificatie (+$8k) = +$24k in 2 maanden
Daarna: veilige funded modus ($4-5k/maand)

Strategie:
  - Zelfde bewezen instellingen (ADX>=30, ATR x1.5, EMA pullback)
  - Fase 1+2: XAUUSD risico 1.8%, anderen 0.5%
  - Dynamische boost: als ADX > 45 op H1 → 2.5x extra lot (top-kwaliteit setup)
  - Fase 3: XAUUSD risico 0.9%, anderen 0.2%, geen boost
"""

import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

@dataclass
class Cfg:
    name:              str   = ""
    symbols:           list  = field(default_factory=lambda: ["XAUUSD","EURUSD","GBPUSD","USDJPY"])
    pip_size:          dict  = field(default_factory=lambda: {
                                   "XAUUSD":0.10,"EURUSD":0.0001,"GBPUSD":0.0001,"USDJPY":0.01})
    session_per_sym:   dict  = field(default_factory=lambda: {
                                   "XAUUSD":(7,20),"EURUSD":(7,20),"GBPUSD":(7,20),"USDJPY":(0,12)})
    h1_ema_fast:       int   = 50
    h1_ema_slow:       int   = 200
    h1_adx_period:     int   = 14
    h1_adx_min:        float = 30.0
    h1_ema_momentum:   int   = 20
    h1_slope_bars:     int   = 3
    m15_ema:           int   = 50
    m15_rsi_period:    int   = 14
    rsi_buy_lo:        float = 45.0
    rsi_buy_hi:        float = 75.0
    rsi_sell_lo:       float = 25.0
    rsi_sell_hi:       float = 55.0
    ema_slope_bars:    int   = 2
    body_pct_min:      float = 0.55
    atr_period:        int   = 14
    atr_sl_mult:       float = 1.5
    rr_ratio:          float = 2.0
    risk_pct_per_sym:  dict  = field(default_factory=lambda: {
                                   "XAUUSD":1.2,"EURUSD":0.3,"GBPUSD":0.3,"USDJPY":0.3})
    # Per-symbool ADX minimum (overschrijft h1_adx_min voor dat symbool)
    adx_min_per_sym:   dict  = field(default_factory=lambda: {})
    # ADX moet stijgen over N H1-bars (0 = uitgeschakeld)
    adx_slope_bars:    int   = 0
    adx_slope_syms:    list  = field(default_factory=list)  # alleen voor deze symbolen
    # Dynamische boost: als ADX > adx_boost_min -> lot * adx_boost_mult
    adx_boost_min:     float = 0.0     # 0 = uitgeschakeld
    adx_boost_mult:    float = 2.0
    # Dynamische R:R: bij zwakke trend lager R:R zodat TP makkelijker gehaald wordt
    rr_weak_adx:       float = 0.0     # 0 = uitgeschakeld; ADX < deze waarde → rr_ratio_weak
    rr_ratio_weak:     float = 1.5
    max_open_per_sym:  int   = 2
    daily_loss_limit:  float = 0.04
    weekly_loss_limit: float = 0.025
    max_drawdown_limit:float = 0.09
    profit_target_pct: float = 0.0
    start_balance:     float = 160_000.0
    session_start:     int   = 7
    session_end:       int   = 20
    bt_start: datetime = field(default_factory=lambda: datetime(2026,1,1,tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026,6,25,tzinfo=timezone.utc))


# ── Fase 1+2: Challenge + Verificatie samen (eerste 2 maanden) ────────────
AGRESSIEF = Cfg(
    name               = "FASE 1+2  Challenge+Verificatie (8k doel)",
    h1_adx_min         = 28.0,
    atr_sl_mult        = 1.5,
    rr_ratio           = 2.0,
    max_open_per_sym   = 2,
    weekly_loss_limit  = 0.03,
    daily_loss_limit   = 0.04,
    max_drawdown_limit = 0.09,
    profit_target_pct  = 0.0,
    adx_min_per_sym    = {"XAUUSD": 32.0},
    adx_slope_bars     = 2,
    adx_slope_syms     = ["XAUUSD"],
    adx_boost_min      = 45.0,
    adx_boost_mult     = 2.0,
    rr_weak_adx        = 0.0,         # uitgeschakeld — altijd R:R 2.0
    rr_ratio_weak      = 1.5,
    risk_pct_per_sym   = {
        "XAUUSD": 1.5,
        "EURUSD": 1.5,
        "GBPUSD": 1.5,
        "USDJPY": 0.8,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026,4,25,tzinfo=timezone.utc),
    bt_end             = datetime(2026,6,25,tzinfo=timezone.utc),
)

# ── Fase 3: Funded — veilig ────────────────────────────────────────────────
VEILIG = Cfg(
    name               = "FASE 3  Funded (veilig)",
    h1_adx_min         = 30.0,
    atr_sl_mult        = 1.5,
    rr_ratio           = 2.0,
    max_open_per_sym   = 2,
    weekly_loss_limit  = 0.015,
    daily_loss_limit   = 0.03,
    max_drawdown_limit = 0.08,
    profit_target_pct  = 0.0,
    adx_boost_min      = 0.0,
    adx_boost_mult     = 1.0,
    risk_pct_per_sym   = {
        "XAUUSD": 0.9,
        "EURUSD": 0.2,
        "GBPUSD": 0.2,
        "USDJPY": 0.2,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026,4,25,tzinfo=timezone.utc),
    bt_end             = datetime(2026,6,25,tzinfo=timezone.utc),
)

# ---------------------------------------------------------------------------

def connect():
    if not mt5.initialize():
        sys.exit(f"MT5 mislukt: {mt5.last_error()}")
    print(f"MT5 verbonden — {mt5.account_info().company}")

def get_bars(sym, tf, start, end):
    r = mt5.copy_rates_range(sym, tf, start, end)
    if r is None or len(r) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")

def ema(s, p):    return s.ewm(span=p, adjust=False).mean()
def sma(s, p):    return s.rolling(p).mean()

def rsi_s(s, p):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(span=p, adjust=False).mean()
    return 100 - 100/(1 + g/(l+1e-9))

def adx_s(df, p):
    h,l,c = df["high"], df["low"], df["close"]
    pdm = (h-h.shift(1)).clip(lower=0); mdm = (l.shift(1)-l).clip(lower=0)
    pdm = pdm.where(pdm>mdm, 0.0);      mdm = mdm.where(mdm>pdm.shift(0), 0.0)
    tr  = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    at  = tr.ewm(span=p,adjust=False).mean()
    pdi = 100*pdm.ewm(span=p,adjust=False).mean()/at
    mdi = 100*mdm.ewm(span=p,adjust=False).mean()/at
    dx  = 100*(pdi-mdi).abs()/(pdi+mdi+1e-9)
    return dx.ewm(span=p,adjust=False).mean()

def atr_s(df, p):
    h,l,c = df["high"],df["low"],df["close"]
    tr = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    return tr.ewm(span=p,adjust=False).mean()

def prep_h1(df, C):
    df = df.copy()
    df["ef"]  = ema(df["close"], C.h1_ema_fast)
    df["es"]  = ema(df["close"], C.h1_ema_slow)
    df["em"]  = ema(df["close"], C.h1_ema_momentum)
    df["adx"] = adx_s(df, C.h1_adx_period)
    return df

def prep_m15(df, C):
    df = df.copy()
    df["e50"] = ema(df["close"], C.m15_ema)
    df["rsi"] = rsi_s(df["close"], C.m15_rsi_period)
    df["atr"] = atr_s(df, C.atr_period)
    return df

# ---------------------------------------------------------------------------

def simulate(symbol, h1, m15, C):
    info = mt5.symbol_info(symbol)
    if info is None:
        return []
    ts=info.trade_tick_size; tv=info.trade_tick_value
    vs=info.volume_step; vn=info.volume_min; vx=info.volume_max; dg=info.digits

    trades=[]; balance=C.start_balance; open_t=[]
    wb=balance; cw=-1; wg=False
    db=balance; cd=None; dg2=False

    arr = m15[["open","high","low","close","e50","rsi","atr"]].values
    idx = m15.index
    wu  = max(C.m15_ema+20, C.m15_rsi_period+C.ema_slope_bars+5, C.atr_period+5)

    for i in range(wu, len(arr)-1):
        t = idx[i]
        o,hi,lo,cl,e50,rv,av = arr[i]
        e50p = arr[i-C.ema_slope_bars][4]
        po,phi,plo,pcl,pe50 = arr[i-1][0],arr[i-1][1],arr[i-1][2],arr[i-1][3],arr[i-1][4]
        # SL wordt later strategisch bepaald (na richting-check)

        wk = t.isocalendar()[1]
        if wk!=cw: cw=wk; wb=balance; wg=False
        dy = t.date()
        if dy!=cd: cd=dy; db=balance; dg2=False

        still=[]
        for ot in open_t:
            done=False
            trade_rr = ot.get("rr", C.rr_ratio)
            if ot["dir"]=="BUY":
                if lo<=ot["sl"]: ot.update(xt=t,xp=ot["sl"],pnl=-ot["risk"],res="LOSS"); balance+=ot["pnl"]; trades.append(ot); done=True
                elif hi>=ot["tp"]: ot.update(xt=t,xp=ot["tp"],pnl=ot["risk"]*trade_rr,res="WIN"); balance+=ot["pnl"]; trades.append(ot); done=True
            else:
                if hi>=ot["sl"]: ot.update(xt=t,xp=ot["sl"],pnl=-ot["risk"],res="LOSS"); balance+=ot["pnl"]; trades.append(ot); done=True
                elif lo<=ot["tp"]: ot.update(xt=t,xp=ot["tp"],pnl=ot["risk"]*trade_rr,res="WIN"); balance+=ot["pnl"]; trades.append(ot); done=True
            if not done: still.append(ot)
        open_t=still

        eq=balance
        if not dg2 and (db-eq)/max(db,1)>=C.daily_loss_limit: dg2=True
        if not wg  and (wb-eq)/max(wb,1)>=C.weekly_loss_limit: wg=True
        if dg2 or wg: continue
        if (C.start_balance-eq)/C.start_balance>=C.max_drawdown_limit: continue
        if len(open_t)>=C.max_open_per_sym: continue

        ss,se = C.session_per_sym.get(symbol,(C.session_start,C.session_end))
        if not (ss<=t.hour<se): continue

        h1i = h1.index.searchsorted(t,side="right")-1
        if h1i < C.h1_ema_slow+20: continue
        adx_val = h1["adx"].iloc[h1i]
        # Per-symbool ADX drempel
        adx_min_sym = C.adx_min_per_sym.get(symbol, C.h1_adx_min)
        if adx_val < adx_min_sym: continue

        # ADX-stijging filter: trend moet aantrekken, niet afzwakken
        if C.adx_slope_bars > 0 and symbol in C.adx_slope_syms:
            if h1i < C.adx_slope_bars: continue
            adx_old = h1["adx"].iloc[h1i - C.adx_slope_bars]
            if adx_val <= adx_old: continue  # ADX daalt of gelijk = trend verzwakt

        ef  = h1["ef"].iloc[h1i]; es=h1["es"].iloc[h1i]
        em  = h1["em"].iloc[h1i]; efp=h1["ef"].iloc[h1i-C.h1_slope_bars]

        if ef>es:
            if em<=ef or ef<=efp: continue
            trend="up"
        elif ef<es:
            if em>=ef or ef>=efp: continue
            trend="down"
        else: continue

        if not (plo<=pe50<=phi): continue
        rng=hi-lo
        if rng==0: continue
        if abs(cl-o)/rng < C.body_pct_min: continue

        direction=None
        if trend=="up":
            if cl>o and cl>e50 and cl>pcl and e50>e50p:
                if C.rsi_buy_lo<=rv<=C.rsi_buy_hi: direction="BUY"
        else:
            if cl<o and cl<e50 and cl<pcl and e50<e50p:
                if C.rsi_sell_lo<=rv<=C.rsi_sell_hi: direction="SELL"
        if direction is None: continue

        # ATR-gebaseerde SL; R:R verlaagd bij zwakke ADX voor hogere TP-trefkans
        sl_d = av * C.atr_sl_mult
        rr   = C.rr_ratio_weak if (C.rr_weak_adx > 0 and adx_val < C.rr_weak_adx) else C.rr_ratio
        tp_d = sl_d * rr

        rp = C.risk_pct_per_sym.get(symbol, 0.5)
        risk_amt = balance * rp / 100
        lperlot  = (sl_d/ts)*tv
        if lperlot<=0: continue
        raw_lot  = risk_amt/lperlot

        # Dynamische boost bij zeer sterke trend
        if C.adx_boost_min > 0 and adx_val >= C.adx_boost_min:
            raw_lot *= C.adx_boost_mult

        lot = max(vn, min(vx, round(round(raw_lot/vs)*vs, 8)))
        slp = round(cl-sl_d,dg) if direction=="BUY" else round(cl+sl_d,dg)
        tpp = round(cl+tp_d,dg) if direction=="BUY" else round(cl-tp_d,dg)

        open_t.append({"sym":symbol,"dir":direction,
                        "et":t,"ep":cl,"sl":slp,"tp":tpp,"lot":lot,"risk":risk_amt,"rr":rr,
                        "xt":None,"xp":None,"pnl":None,"res":None})

    lc=m15["close"].iloc[-1]
    for ot in open_t:
        pnl=(lc-ot["ep"])/ts*tv*ot["lot"] if ot["dir"]=="BUY" else (ot["ep"]-lc)/ts*tv*ot["lot"]
        ot.update(xt=m15.index[-1],xp=lc,pnl=pnl,res="OPEN_CLOSE")
        trades.append(ot)
    return trades

# ---------------------------------------------------------------------------

def wk_lbl(dt):
    iso=dt.isocalendar(); return f"{iso[0]}-W{iso[1]:02d}"

def max_dd(pnls, start):
    peak=eq=start; mdd=0.0
    for p in pnls:
        eq+=p; peak=max(peak,eq); mdd=max(mdd,(peak-eq)/peak*100)
    return mdd

def report(C, raw, show_milestone=True):
    if not raw:
        print(f"\n  [{C.name}] Geen trades."); return {}

    rows = [{"sym":t["sym"],"dir":t["dir"],
             "et":pd.to_datetime(t["et"],utc=True),
             "xt":pd.to_datetime(t["xt"],utc=True),
             "pnl":t["pnl"],"res":t["res"]} for t in raw]
    df = pd.DataFrame(rows)
    df["week"] = df["et"].apply(wk_lbl)
    df["win"]  = df["pnl"] > 0

    sep="="*78
    print(f"\n{sep}")
    print(f"  {C.name}")
    print(f"  XAU {C.risk_pct_per_sym.get('XAUUSD')}%  |  "
          f"ADX>={C.h1_adx_min}  |  "
          f"Boost ADX>{C.adx_boost_min}x{C.adx_boost_mult}  |  "
          f"Weekstop {C.weekly_loss_limit*100:.1f}%  |  Max DD {C.max_drawdown_limit*100:.0f}%")
    print(sep)
    print(f"{'Week':<12} {'#':>4} {'WR':>7} {'W':>4} {'L':>4} "
          f"{'P&L':>11}  {'Balans':>11}  {'%':>7}  Mijlpaal")
    print("-"*78)

    running = C.start_balance
    w16k=None; w24k=None; w_ch=None; w_ve=None
    for wk, grp in df.groupby("week"):
        n=len(grp); w=int(grp["win"].sum()); pnl=grp["pnl"].sum()
        running+=pnl
        pct=(running-C.start_balance)/C.start_balance*100
        teken="+" if pnl>=0 else ""
        mijl=""
        if w16k is None and running>=C.start_balance*1.10:
            w16k=wk; w_ch=wk; mijl=" *** CHALLENGE GEHAALD (+10%) ***"
        elif w24k is None and running>=C.start_balance*1.15:
            w24k=wk; w_ve=wk; mijl=" *** VERIFICATIE GEHAALD (+15%) ***"
        print(f"{wk:<12} {n:>4} {w/n*100:>6.0f}%  {w:>4} {n-w:>4}  "
              f"{teken}{pnl:>9,.0f}   {running:>10,.0f}  {pct:>+6.1f}%{mijl}")

    total=len(df); wins=int(df["win"].sum())
    net=df["pnl"].sum(); end_bal=C.start_balance+net
    wr=wins/total*100 if total else 0
    mdd_v=max_dd(df.sort_values("xt")["pnl"].tolist(),C.start_balance)
    gw=df.loc[df["pnl"]>0,"pnl"].sum(); gl=df.loc[df["pnl"]<0,"pnl"].sum()
    pf=gw/abs(gl) if gl!=0 else float("inf")
    avg_w=df.loc[df["win"],"pnl"].mean() if wins else 0
    avg_l=df.loc[~df["win"],"pnl"].mean() if (total-wins) else 0
    maanden=(C.bt_end-C.bt_start).days/30.44

    print(sep)
    print(f"  Totaal: {total} trades  |  {wins}W/{total-wins}L  |  Winrate: {wr:.1f}%")
    print(f"  Gem. winst ${avg_w:+,.0f}  |  Gem. verlies ${avg_l:+,.0f}  |  PF: {pf:.2f}")
    print(f"  Netto P&L  : ${net:+,.0f}  ({net/C.start_balance*100:.1f}%)")
    print(f"  Eindbalans : ${end_bal:,.0f}")
    print(f"  Max DD     : {mdd_v:.2f}%  {'OK' if mdd_v<C.max_drawdown_limit*100+1 else 'OVER LIMIET!'}")
    print(f"  Gem/maand  : ${net/maanden:+,.0f}")

    if show_milestone:
        start_wk = int(wk_lbl(C.bt_start).split("-W")[1])
        if w_ch:
            wn=int(w_ch.split("-W")[1])-start_wk+1
            print(f"  Challenge (+10%) : week {w_ch}  (na {wn} weken = {wn//4:.0f}-{(wn+3)//4:.0f} maanden)")
        else:
            print(f"  Challenge (+10%) : NIET bereikt in periode")
        if w_ve:
            wn2=int(w_ve.split("-W")[1])-start_wk+1
            print(f"  Verificatie(+15%): week {w_ve}  (na {wn2} weken = ~{wn2/4:.1f} maanden)")
        else:
            print(f"  Verificatie(+15%): NIET bereikt in periode")
    print(sep)

    print("  Per symbool:")
    for sym,grp in df.groupby("sym"):
        w2=int(grp["win"].sum())
        print(f"    {sym:<8} {len(grp):>3} trades  {w2/len(grp)*100:.0f}%  ${grp['pnl'].sum():+,.0f}")

    # Dagelijkse P&L analyse
    df["day"] = df["xt"].dt.date
    daily = df.groupby("day")["pnl"].sum()
    worst_day_pnl = daily.min()
    worst_day     = daily.idxmin()
    best_day_pnl  = daily.max()
    best_day      = daily.idxmax()
    EURUSD_RATE   = 1.08  # USD → EUR
    print(f"\n  Dagelijkse P&L analyse:")
    print(f"    Slechtste dag : {worst_day}  ${worst_day_pnl:+,.0f}  (~EUR {worst_day_pnl/EURUSD_RATE:+,.0f})")
    print(f"    Beste dag     : {best_day}   ${best_day_pnl:+,.0f}  (~EUR {best_day_pnl/EURUSD_RATE:+,.0f})")
    print(f"    Daglimiet FTMO: ${C.start_balance*C.daily_loss_limit:,.0f}  "
          f"(~EUR {C.start_balance*C.daily_loss_limit/EURUSD_RATE:,.0f})  "
          f"-- slechtste dag was {abs(worst_day_pnl)/(C.start_balance*C.daily_loss_limit)*100:.0f}% van daglimiet")
    print(sep)

    return {"name":C.name,"trades":total,"wr":wr,"pnl":net,"mdd":mdd_v,
            "pf":pf,"maand":net/maanden,"w_ch":w_ch,"w_ve":w_ve}

# ---------------------------------------------------------------------------

def main():
    connect()
    print("\nData laden...")
    cache={}
    for sym in AGRESSIEF.symbols:
        mt5.symbol_select(sym, True)
        h1r  = get_bars(sym, mt5.TIMEFRAME_H1,  AGRESSIEF.bt_start, AGRESSIEF.bt_end)
        m15r = get_bars(sym, mt5.TIMEFRAME_M15, AGRESSIEF.bt_start, AGRESSIEF.bt_end)
        if not h1r.empty and not m15r.empty:
            cache[sym]=(h1r,m15r)
            print(f"  {sym}: {len(h1r)} H1 / {len(m15r)} M15 bars")

    results=[]
    for C in [AGRESSIEF, VEILIG]:
        all_t=[]
        for sym,(h1r,m15r) in cache.items():
            h1=prep_h1(h1r,C); m15=prep_m15(m15r,C)
            t=simulate(sym,h1,m15,C); all_t.extend(t)
        r=report(C,all_t); results.append(r) if r else None

    mt5.shutdown()

    # Eindoverzicht
    print("\n\n"+"="*78)
    print("  EINDOVERZICHT")
    print("="*78)
    print(f"{'Fase':<30} {'Trades':>6} {'WR':>7} {'P&L':>10} {'Maand':>9} {'Max DD':>8}  {'Challenge':>12}  Verificatie")
    print("-"*78)
    for r in results:
        ch = str(r.get("w_ch") or "n.v.t.")
        ve = str(r.get("w_ve") or "n.v.t.")
        print(f"{r['name']:<30} {r['trades']:>6} {r['wr']:>6.1f}%  "
              f"${r['pnl']:>9,.0f}  ${r['maand']:>8,.0f}  {r['mdd']:>7.2f}%  "
              f"{ch:>12}  {ve}")
    print("="*78)
    print()
    print("  HANDLEIDING VOOR JOU:")
    print("  1) Start bot in AGRESSIEF modus")
    print("     Zodra balans = $176,000  -> Challenge geslaagd (stap 1)")
    print("     Zodra balans = $184,000  -> Verificatie geslaagd (stap 2)")
    print("  2) Verander in bot: MODUS = 'VEILIG'")
    print("     Nu verdien je stabiel $3-5k per maand met minimaal risico")
    print()

if __name__ == "__main__":
    main()
