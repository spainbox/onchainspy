#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nansen Listener — FORWARD y BACKTEST (Telegram directo, sin ficheros intermedios)
Versión: v0.9.0-mcap-weights (conservadora y sin simplificaciones destructivas)

- FORWARD:
  * Lee mensajes nuevos del chat `NANSEN_CHAT_NAME` (Telethon).
  * Calcula “confianza” (presión de venta/compra) por token en 1h / 4h / 24h.
  * Envía al canal si |conf-50| >= REPORT_DEVIATION en algún timeframe.
  * Escribe snapshot_latest.json y snapshots_history.jsonl.

- BACKTEST (Telegram directo):
  * Descarga histórico desde BACKTEST_SINCE hasta ahora (máx. SEED_HISTORY_LIMIT).
  * Reproduce en memoria (pasos SNAPSHOT_EVERY_SEC) aplicando MIN_LAG_MINUTES
    para ignorar el impacto inmediato.
  * Opción REPLAY_SEED_SNAPSHOTS=1 para escribir histórico durante el replay.

- Presión/Confianza:
  1) Parseamos cada alerta a {token, flujo: CEX_IN, CEX_OUT, DEX, VC_IN, VC_OUT, MERCADO, usd_amount}.
  2) Aplicamos peso por flujo: pressure = usd_amount * WEIGHTS_KIND_JSON[flujo].
     Semántica: presión > 0 ⇒ VENDEDORA; presión < 0 ⇒ COMPRADORA.
  3) Normalizamos por tamaño de mercado: pressure_norm = pressure * (1e6 / MARKET_CAP_USD[token]).
  4) En cada ventana, confianza = 50 + 50 * tanh( sum(pressure_norm) / S ),
     con S = 10 * mediana(|pressure_norm_eventos_en_ventana|)  (fallback S=1.0).
  5) Ignoramos eventos con ts > (now_utc - MIN_LAG_MINUTES) para evitar “efecto inmediato”.

- Env:
  Ver .env (variables listadas al final).
"""

import os
import re
import json
import math
import time
import asyncio
import datetime as dt
from statistics import median
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# -----------------------------
# Entorno / Paths
# -----------------------------
load_dotenv(override=True)

OUT_DIR                 = os.getenv("OUT_DIR", "out")
os.makedirs(OUT_DIR, exist_ok=True)
SNAPSHOT_LATEST_PATH    = os.path.join(OUT_DIR, "snapshot_latest.json")
SNAPSHOT_HISTORY_PATH   = os.path.join(OUT_DIR, "snapshots_history.jsonl")

MODE                    = os.getenv("MODE", "FORWARD").upper().strip()
BACKTEST_SINCE_STR      = os.getenv("BACKTEST_SINCE", "2025-08-26 00:00:00")
REPLAY_SEED_SNAPSHOTS   = int(os.getenv("REPLAY_SEED_SNAPSHOTS", "1"))
SNAPSHOT_EVERY_SEC      = int(os.getenv("SNAPSHOT_EVERY_SEC", "300"))

TIMEZONE_OFFSET_HOURS   = int(os.getenv("TIMEZONE_OFFSET_HOURS", "0"))
TZ_PRINT                = dt.timezone(dt.timedelta(hours=TIMEZONE_OFFSET_HOURS))

TELEGRAM_API_ID         = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH       = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION        = os.getenv("TELEGRAM_SESSION", "nansen_reader")
NANSEN_CHAT_NAME        = os.getenv("NANSEN_CHAT_NAME", "NansenBot")

BOT_TOKEN               = os.getenv("BOT_TOKEN", "")
CHANNEL_CHAT_ID         = os.getenv("CHANNEL_CHAT_ID", "")

TOKENS_CSV              = os.getenv("TOKENS", "AAVE,LINK,HYPE,ETH,STABLES")
TOKENS                  = [t.strip().upper() for t in TOKENS_CSV.split(",") if t.strip()]

REPORT_DEVIATION        = int(os.getenv("REPORT_DEVIATION", "40"))
STARTUP_REPORT          = int(os.getenv("STARTUP_REPORT", "1"))
BREAKDOWN_IN_CHANNEL    = int(os.getenv("BREAKDOWN_IN_CHANNEL", "0"))

WRITE_SNAPSHOT          = int(os.getenv("WRITE_SNAPSHOT", "1"))
WRITE_HISTORY           = int(os.getenv("WRITE_HISTORY", "1"))

VERBOSE_BREAKDOWN       = int(os.getenv("VERBOSE_BREAKDOWN", "1"))
BREAKDOWN_WINDOWS_CSV   = os.getenv("BREAKDOWN_WINDOWS", "1h,4h,24h")
BREAKDOWN_WINDOWS       = [w.strip() for w in BREAKDOWN_WINDOWS_CSV.split(",") if w.strip()]
MAX_BREAKDOWN_LINES     = int(os.getenv("MAX_BREAKDOWN_LINES", "30"))

SEED_FROM_HISTORY       = int(os.getenv("SEED_FROM_HISTORY", "1"))
SEED_HISTORY_HOURS      = int(os.getenv("SEED_HISTORY_HOURS", "48"))
SEED_HISTORY_LIMIT      = int(os.getenv("SEED_HISTORY_LIMIT", "3000"))

# ---- NUEVO: Pesos, lag y market caps ----
def _load_json_env(name: str, default_str: str) -> Dict:
    txt = os.getenv(name, default_str).strip()
    try:
        return json.loads(txt)
    except Exception as e:
        raise RuntimeError(f"{name} en .env no es JSON válido: {e}")

WEIGHTS_KIND: Dict[str, float] = _load_json_env(
    "WEIGHTS_KIND_JSON",
    '{"CEX_IN":1.5,"CEX_OUT":-1.0,"DEX":0.5,"VC_IN":1.2,"VC_OUT":-0.7,"MERCADO":0.3}'
)
# Nota: he puesto VC_OUT=-0.7 (compradora) por coherencia direccional; si quieres dejar +0.7, cámbialo en .env.

MIN_LAG_MINUTES         = int(os.getenv("MIN_LAG_MINUTES", "5"))

MARKET_CAPS: Dict[str, float] = _load_json_env(
    "MARKET_CAP_USD",
    '{"AAVE":1200000000,"LINK":9000000000,"HYPE":200000000,"ETH":400000000000}'
)

# ---- THRESHOLDS_JSON se mantiene (para mínimos por tipo si lo usas en otros módulos) ----
def _load_thresholds() -> Dict:
    txt = os.getenv("THRESHOLDS_JSON", "").strip()
    if not txt:
        return {
            "*": {"CEX":{"min_tx_usd":150000},
                  "DEX":{"min_tx_usd":250000},
                  "VC":{"min_tx_usd":1000000},
                  "MERCADO":{"min_tx_usd":0}},
            "AAVE":{"CEX":{"min_tx_usd":150000}},
            "LINK":{},
            "HYPE":{},
            "ETH": {},
        }
    try:
        return json.loads(txt)
    except Exception:
        raise RuntimeError("THRESHOLDS_JSON en .env no es JSON válido. Revísalo.")

THRESHOLDS = _load_thresholds()

# -----------------------------
# Regex
# -----------------------------
RE_TOKEN_ANY = re.compile(r"\b([A-Z]{2,10})\b")
RE_KIND      = re.compile(r"\b(CEX|DEX|VC|MERCADO)\b", re.I)
RE_EXCHANGE  = re.compile(r"\[([A-Za-z0-9\.\-_ ]{2,30})\]")
RE_USD       = re.compile(r"([\-+]?)\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,4})?)")
RE_HHMMSS    = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d:[0-5]\d\b", re.I)

# -----------------------------
# Estructuras
# -----------------------------
@dataclass
class Event:
    ts: dt.datetime     # UTC
    token: str
    flow: str           # CEX_IN/CEX_OUT/DEX/VC_IN/VC_OUT/MERCADO
    usd_amount: float   # SIEMPRE USD absolutos (>0), nunca tokens
    exchange: str       # opcional
    raw: str            # línea cruda

# -----------------------------
# Utilidades de tiempo
# -----------------------------
def parse_since(s: str) -> dt.datetime:
    s = s.replace("T", " ")
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)

def fmt_stamp_with_tz(t: dt.datetime) -> str:
    local = t.astimezone(TZ_PRINT)
    suffix = f"UTC{TIMEZONE_OFFSET_HOURS:+d}"
    return local.strftime("%Y-%m-%d %H:%M:%S ") + suffix

# -----------------------------
# Parser y clasificación de flujo
# -----------------------------
def pick_token_from_text(text: str) -> Optional[str]:
    up = text.upper()
    present = [tok for tok in TOKENS if tok in up]
    if present:
        return present[0]
    m = RE_TOKEN_ANY.search(up)
    return m.group(1) if m else None

def classify_flow(base_kind: str, raw_line_upper: str) -> str:
    k = base_kind.upper()
    u = raw_line_upper
    if k == "CEX":
        # Por defecto depósito a CEX => CEX_IN (presión vendedora)
        if ("WITHDRAW" in u) or ("OUTFLOW FROM CEX" in u) or ("WITHDRAWAL" in u):
            return "CEX_OUT"
        return "CEX_IN"
    if k == "VC":
        if "INFLOW" in u:
            return "VC_IN"
        if "OUTFLOW" in u:
            return "VC_OUT"
        # sin contexto: asumimos VC_IN (carga para vender) como hipótesis conservadora
        return "VC_IN"
    if k == "DEX":
        # Si puedes detectar SELL/BUY, puedes derivar signo; aquí tratamos DEX como “neutro de dirección”
        # y dejamos la semántica al peso DEX en WEIGHTS_KIND_JSON.
        return "DEX"
    return "MERCADO"

def parse_events_from_message(msg_text: str, msg_date_utc: dt.datetime) -> List[Event]:
    rows = []
    token = pick_token_from_text(msg_text)
    if not token:
        return rows

    up_all = msg_text.upper()
    for line in msg_text.splitlines():
        km = RE_KIND.search(line)
        um = RE_USD.search(line.replace(",", ""))
        if not km or not um:
            continue
        base_kind = km.group(1).upper()
        usd_val = float(um.group(2).replace(",", ""))
        if usd_val <= 0:
            continue

        exch = ""
        em = RE_EXCHANGE.search(line)
        if em:
            exch = em.group(1).strip()

        # timestamp si viene en la línea
        ts_line = msg_date_utc
        hm = RE_HHMMSS.search(line)
        if hm:
            h, m, s = map(int, hm.group(0).split(":"))
            base = msg_date_utc.astimezone(dt.timezone.utc)
            ts_line = base.replace(hour=h, minute=m, second=s, microsecond=0)

        flow = classify_flow(base_kind, line.upper())
        rows.append(Event(ts=ts_line, token=token, flow=flow, usd_amount=usd_val, exchange=exch, raw=line))
    return rows

# -----------------------------
# Ponderaciones y normalización
# -----------------------------
def get_market_cap(token: str) -> float:
    return float(MARKET_CAPS.get(token.upper(), 1_000_000_000))  # fallback 1B

def weight_for_flow(flow: str) -> float:
    return float(WEIGHTS_KIND.get(flow, 0.0))

def pressure_usd(ev: Event) -> float:
    # presión firmada en USD (peso incorpora la dirección)
    return ev.usd_amount * weight_for_flow(ev.flow)

def normalize_pressure(token: str, pressure: float) -> float:
    mc = max(1.0, get_market_cap(token))
    return pressure * (1_000_000.0 / mc)

# -----------------------------
# Ventanas: agregación y confianza
# -----------------------------
WINDOWS = {"1h":1, "4h":4, "24h":24}

def events_in_window(events: List[Event], now_utc: dt.datetime, hours: int) -> List[Event]:
    tmin = now_utc - dt.timedelta(hours=hours)
    # aplica lag mínimo (ev.ts <= now_utc - MIN_LAG_MINUTES)
    latest_ok = now_utc - dt.timedelta(minutes=MIN_LAG_MINUTES)
    return [e for e in events if tmin <= e.ts <= latest_ok]

def calc_conf_from_pressures(norm_pressures: List[float]) -> Tuple[int, float]:
    total = sum(norm_pressures)
    # escala dinámica robusta: mediana(|x|) * 10
    abs_vals = [abs(x) for x in norm_pressures if x != 0]
    if abs_vals:
        S = max(1.0, median(abs_vals) * 10.0)
    else:
        S = 1.0
    conf = 50.0 + 50.0 * math.tanh(total / S)
    return int(round(conf)), total

def aggregate_by_window(events: List[Event], now_utc: dt.datetime) -> Dict[str, Dict[str, Dict]]:
    out: Dict[str, Dict[str, Dict]] = {t:{w:{"conf":50,"events":0,"usd":0.0} for w in WINDOWS} for t in TOKENS}

    for wlab, wh in WINDOWS.items():
        evs_w = events_in_window(events, now_utc, wh)
        # agrupa por token
        bucket_norm: Dict[str, List[float]] = defaultdict(list)
        counts: Dict[str, int] = defaultdict(int)
        sum_pressure_usd: Dict[str, float] = defaultdict(float)  # solo para info ΣUSD (firmado por pesos)

        for ev in evs_w:
            p = pressure_usd(ev)                         # USD * peso (firmado)
            pn = normalize_pressure(ev.token, p)         # normalizado por market cap
            bucket_norm[ev.token].append(pn)
            counts[ev.token] += 1
            sum_pressure_usd[ev.token] += p

        for token in TOKENS:
            conf, _total_norm = calc_conf_from_pressures(bucket_norm.get(token, []))
            out[token][wlab]["conf"]   = conf
            out[token][wlab]["events"] = counts.get(token, 0)
            # “usd” de salida = suma de presiones en USD (con peso y signo) redondeada solo para diagnóstico
            out[token][wlab]["usd"]    = float(round(sum_pressure_usd.get(token, 0.0), 2))
    return out

def breakdowns_by_window(events: List[Event], now_utc: dt.datetime, max_lines: int = 100) -> Dict[str, Dict[str, Dict]]:
    out: Dict[str, Dict[str, Dict]] = {}
    for token in TOKENS:
        out[token] = {}
        for wlab, wh in WINDOWS.items():
            seq = [ev for ev in events_in_window(events, now_utc, wh) if ev.token == token]
            seq.sort(key=lambda e: e.ts)

            # escala S de esta ventana/token
            prelim_norm = [normalize_pressure(token, pressure_usd(ev)) for ev in seq]
            abs_vals = [abs(x) for x in prelim_norm if x != 0]
            S = max(1.0, median(abs_vals) * 10.0) if abs_vals else 1.0

            # construir items con cálculo incremental (para conf_after y % de impacto)
            items = []
            cum_norm = 0.0
            total_norm = sum(prelim_norm)
            total_norm_abs = sum(abs(x) for x in prelim_norm)

            for ev in seq:
                p_usd = pressure_usd(ev)
                p_n   = normalize_pressure(token, p_usd)
                cum_norm += p_n
                conf_after = 50.0 + 50.0 * math.tanh(cum_norm / S)
                pct = (abs(p_n) / total_norm_abs * 100.0) if total_norm_abs > 0 else 0.0
                items.append({
                    "ts": ev.ts.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00","Z"),
                    "kind": ev.flow,
                    "usd": float(round(p_usd, 2)),               # presión en USD con signo (peso aplicado)
                    "usd_amount": float(round(ev.usd_amount, 2)),# USD bruto del evento (sin peso)
                    "weight": float(round(weight_for_flow(ev.flow), 4)),
                    "pressure": float(round(p_usd, 2)),          # alias de usd (por compat)
                    "pressure_norm": float(round(p_n, 8)),
                    "pct_norm": float(round(pct, 2)),
                    "delta_conf": "",                            # mantenemos clave por compat (no delta puntual)
                    "conf_after": f"{conf_after:.1f}",
                    "exchange": ev.exchange or ""
                })

            # Totales ventana (usar aggregate_by_window para conf/eventos/ΣUSD)
            agg_tmp = aggregate_by_window(events, now_utc)  # usa MIN_LAG y todo
            conf_total   = agg_tmp[token][wlab]["conf"]
            events_total = len(seq)
            usd_total    = float(round(sum(pressure_usd(e) for e in seq), 2))

            if max_lines > 0 and len(items) > max_lines:
                items = items[-max_lines:]

            out[token][wlab] = {
                "conf": conf_total,
                "events": events_total,
                "usd": usd_total,
                "events_list": items
            }
    return out

# -----------------------------
# Formateo de snapshot (texto)
# -----------------------------
def fmt_snapshot_text(agg: Dict, bks: Dict, now_utc: dt.datetime) -> str:
    lines = []
    lines.append("🟢 Diagnóstico:")
    for token in TOKENS:
        lines.append(f"🔎 {token} — Interpretación de alertas")
        lines.append(f"📅 {fmt_stamp_with_tz(now_utc)}")
        for wlab in ("1h","4h","24h"):
            conf = agg[token][wlab]["conf"]
            events = agg[token][wlab]["events"]
            usd = agg[token][wlab]["usd"]
            tag = "Compra" if conf < 45 else ("Venta" if conf > 55 else "Neutro")
            sign = "$" if usd >= 0 else "-$"
            lines.append(f"• {wlab} → {tag} (confianza {conf}/100)  eventos={events}, Σ={sign}{abs(usd):,.2f}")
        if VERBOSE_BREAKDOWN:
            for wlab in BREAKDOWN_WINDOWS:
                if wlab not in ("1h","4h","24h"):
                    continue
                win = bks[token].get(wlab) or {}
                lines.append(f"\n📊 Desglose {wlab}:")
                items = win.get("events_list") or []
                if not items:
                    lines.append("  (sin contribuciones)")
                else:
                    for it in items[-MAX_BREAKDOWN_LINES:]:
                        ts_ = dt.datetime.fromisoformat(it["ts"].replace("Z","+00:00")).astimezone(TZ_PRINT)
                        hhmmss = ts_.strftime("%H:%M:%S")
                        lines.append(
                          f"  • {hhmmss} {it['kind']} "
                          f"USD={it['usd_amount']:,.2f} w={it['weight']} "
                          f"P={it['pressure']:,.2f} P̂={it['pressure_norm']:.6f} "
                          f"(%={it['pct_norm']:.1f}) ⇒ {it['conf_after']}"
                          + (f" [{it['exchange']}]" if it.get('exchange') else "")
                        )
        lines.append("")
    return "\n".join(lines).strip()

# -----------------------------
# IO: snapshot / history
# -----------------------------
def write_snapshot_file(now_utc: dt.datetime, agg: Dict, bks: Dict, snap_text: str):
    if not WRITE_SNAPSHOT:
        return
    payload = {
        "ts_utc": now_utc.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00","Z"),
        "ts_local": now_utc.astimezone(TZ_PRINT).isoformat(),
        "timezone_suffix": f"UTC{TIMEZONE_OFFSET_HOURS:+d}",
        "tokens": TOKENS,
        "agg": agg,
        "breakdowns": bks,
        "snap_text": snap_text
    }
    with open(SNAPSHOT_LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"💾 Snapshot escrito: {SNAPSHOT_LATEST_PATH}")

def append_history(now_utc: dt.datetime, agg: Dict):
    if not WRITE_HISTORY:
        return
    row = {"ts_utc": now_utc.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00","Z"), "agg": agg}
    with open(SNAPSHOT_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"🧾 Histórico anexado: {SNAPSHOT_HISTORY_PATH}")

# -----------------------------
# Envío a canal
# -----------------------------
import requests
def send_to_channel(text: str):
    tok = BOT_TOKEN.strip()
    chat = CHANNEL_CHAT_ID.strip()
    if not tok or not chat:
        print("ℹ️ Canal no configurado o envío desactivado.")
        return
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat, "text": text, "parse_mode":"HTML"})
    if not resp.ok:
        print("⚠️ Fallo al enviar al canal:", resp.status_code, resp.text)

def should_send(agg: Dict) -> bool:
    for token in TOKENS:
        for wlab in ("1h","4h","24h"):
            conf = agg[token][wlab]["conf"]
            if abs(conf - 50) >= REPORT_DEVIATION:
                return True
    return False

# -----------------------------
# Telegram
# -----------------------------
async def fetch_from_telegram(since_utc: dt.datetime, limit: int) -> List[Tuple[dt.datetime, str]]:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("Faltan TELEGRAM_API_ID/TELEGRAM_API_HASH en .env")

    client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    entity = await client.get_entity(NANSEN_CHAT_NAME)

    out = []
    async for msg in client.iter_messages(entity, limit=limit, reverse=True):
        if not msg.message:
            continue
        ts = msg.date
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        else:
            ts = ts.astimezone(dt.timezone.utc)
        if ts < since_utc:
            continue
        out.append((ts, msg.message))
    await client.disconnect()
    out.sort(key=lambda x: x[0])
    return out

# -----------------------------
# MAIN
# -----------------------------
def main():
    print("🚀 Leyendo historial…")
    events: List[Event] = []

    if MODE == "BACKTEST":
        since = parse_since(BACKTEST_SINCE_STR)
        msgs = asyncio.run(fetch_from_telegram(since, SEED_HISTORY_LIMIT))
        if not msgs:
            print("⚠️ BACKTEST: no se recuperó historial de Telegram (¿sin mensajes o filtros?).")
        else:
            print(f"✅ Semilla: {len(msgs)} mensajes desde {since.isoformat()} UTC.")

        for (ts, text) in msgs:
            events.extend(parse_events_from_message(text, ts))

        if not events:
            now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            agg = aggregate_by_window([], now_utc)
            bks = breakdowns_by_window([], now_utc, MAX_BREAKDOWN_LINES)
            snap_txt = fmt_snapshot_text(agg, bks, now_utc)
            write_snapshot_file(now_utc, agg, bks, snap_txt)
            append_history(now_utc, agg)
            if should_send(agg):
                payload = "🟢 Diagnóstico (BACKTEST)\n\n" + snap_txt
                send_to_channel(payload if BREAKDOWN_IN_CHANNEL else "\n".join(snap_txt.splitlines()[:5]))
            else:
                print("ℹ️ No enviado al canal (desviación por debajo de umbral).")
            return

        events.sort(key=lambda e: e.ts)
        # Alinea el puntero a múltiplos del paso
        t0 = events[0].ts.replace(minute=(events[0].ts.minute // (SNAPSHOT_EVERY_SEC//60))*(SNAPSHOT_EVERY_SEC//60),
                                  second=0, microsecond=0)
        tN = events[-1].ts
        pointer = t0
        print(f"♻️ REPLAY {t0.isoformat()} .. {tN.isoformat()} step={SNAPSHOT_EVERY_SEC}s  (lag={MIN_LAG_MINUTES}m)")

        while pointer <= tN:
            upto = [e for e in events if e.ts <= pointer]
            agg = aggregate_by_window(upto, pointer)
            bks = breakdowns_by_window(upto, pointer, MAX_BREAKDOWN_LINES)
            snap_txt = fmt_snapshot_text(agg, bks, pointer)
            write_snapshot_file(pointer, agg, bks, snap_txt)
            if REPLAY_SEED_SNAPSHOTS:
                append_history(pointer, agg)
            if should_send(agg):
                payload = "🟢 Diagnóstico (BACKTEST)\n\n" + snap_txt
                send_to_channel(payload if BREAKDOWN_IN_CHANNEL else "\n".join(snap_txt.splitlines()[:5]))
            pointer += dt.timedelta(seconds=SNAPSHOT_EVERY_SEC)
        return

    # -------- FORWARD --------
    since_seed = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=SEED_HISTORY_HOURS)
    msgs = []
    if SEED_FROM_HISTORY:
        msgs = asyncio.run(fetch_from_telegram(since_seed, SEED_HISTORY_LIMIT))
        print(f"✅ Semilla: {len(msgs)} mensajes en {SEED_HISTORY_HOURS}h.")
    else:
        print("ℹ️ Sin semilla inicial.")

    for (ts, text) in msgs:
        events.extend(parse_events_from_message(text, ts))

    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    agg = aggregate_by_window(events, now_utc)
    bks = breakdowns_by_window(events, now_utc, MAX_BREAKDOWN_LINES)
    snap_txt = fmt_snapshot_text(agg, bks, now_utc)
    print(snap_txt)
    write_snapshot_file(now_utc, agg, bks, snap_txt)
    append_history(now_utc, agg)

    if STARTUP_REPORT and should_send(agg):
        payload = "🟢 Diagnóstico (ARRANQUE)\n\n" + snap_txt
        send_to_channel(payload if BREAKDOWN_IN_CHANNEL else "\n".join(snap_txt.splitlines()[:5]))
    else:
        print("ℹ️ No enviado al canal (desviación por debajo de umbral o STARTUP_REPORT=0).")

if __name__ == "__main__":
    main()