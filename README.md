# Nansen Listener + Dashboard

Este proyecto contiene dos componentes principales:

1. **Listener (`nansen_listener1.py`)**  
   - Conecta directamente al canal de Telegram del bot de Nansen (`NANSEN_CHAT_NAME`).  
   - Puede funcionar en dos modos:
     - **FORWARD**: escucha y procesa mensajes recientes en tiempo real (one-shot).  
     - **BACKTEST**: descarga histórico desde `BACKTEST_SINCE`, lo parsea y reproduce la evolución paso a paso.
   - Calcula métricas de **confianza/presión de compra-venta** en ventanas de 1h, 4h y 24h.
   - Genera salidas:
     - Diagnóstico en consola.
     - Envío opcional a un canal privado de Telegram (`BOT_TOKEN`, `CHANNEL_CHAT_ID`).
     - Ficheros `out/snapshot_latest.json` y `out/snapshots_history.jsonl`.

2. **Dashboard (`nansen_dashboard.py`)**  
   - Interfaz visual en **Streamlit**.  
   - Lee los snapshots generados por el listener.  
   - Muestra:
     - Snapshot actual (confianza por token en 1h/4h/24h).
     - Desglose de contribuciones por evento con su delta de confianza.
     - Histórico de confianza por token y timeframe (gráficas de líneas simultáneas).  
   - Permite explorar tokens y comparar evolución de señales.

---

## 📊 Algoritmo de confianza/presión

- Cada evento se parsea en base a:
  - **Token** (AAVE, LINK, HYPE, ETH, STABLES…).
  - **Kind** (CEX, DEX, VC, MERCADO).
  - **USD firmado** (positivo = compra/acumulación, negativo = venta/distribución).

- La presión se acumula por ventana de tiempo (1h, 4h, 24h).  
- Fórmula principal:

```python
conf = 50 + 50 * tanh( K * ( signed_usd_sum / baseline ) )
```

- **Interpretación:**
  - `conf = 100`: máxima compra/acumulación.
  - `conf = 0`: máxima venta/distribución.
  - `conf = 50`: neutro.

- Parámetros clave:
  - `K` configurable por token en `.env` (`CONF_SCALER_K_JSON`).
  - `baseline`: referencia dinámica = `min_tx_usd(token, tipo) * horas` o percentil (`BASELINE_MODE=PCTL`).
  - **NUEVO**: ponderación adicional por capitalización:  
    ```python
    MARKET_CAP_USD={"AAVE":1.2e9,"LINK":9e9,"HYPE":2e8,"ETH":4e11}
    ```
    Normaliza el impacto de cada operación según el tamaño relativo del token.

- Soporta ponderaciones específicas de cada categoría (ejemplo en `.env`):  
```python
WEIGHTS_KIND_JSON={
  "CEX_IN":1.5,
  "CEX_OUT":-1.0,
  "DEX":0.5,
  "VC_IN":1.2,
  "VC_OUT":0.7,
  "MERCADO":0.3
}
```

---

## 📂 Ficheros de salida

- `out/snapshot_latest.json`: último snapshot completo.
- `out/snapshots_history.jsonl`: histórico incremental (1 línea por snapshot).

### JSON Schema (history rows)

```json
{
  "ts_utc": "2025-08-29T20:00:00Z",
  "agg": {
    "AAVE": {
      "1h": {"conf":90,"events":34,"usd":835994.55},
      "4h": {"conf":82,"events":45,"usd":2297993.69},
      "24h":{"conf":56,"events":45,"usd":2297993.69}
    },
    "LINK": {...},
    "HYPE": {...},
    "ETH": {...},
    "STABLES": {...}
  }
}
```

### JSON Schema (latest snapshot)

```json
{
  "ts_utc": "...",
  "ts_local": "...",
  "timezone_suffix": "UTC+2",
  "tokens": ["AAVE","LINK","HYPE","ETH","STABLES"],
  "agg": {...},
  "breakdowns": {...},
  "snap_text": "texto formateado diagnóstico"
}
```

---

## ⚙️ Variables de entorno `.env`

```ini
# ---- MODO ----
MODE=FORWARD                  # FORWARD o BACKTEST
BACKTEST_SINCE=2025-08-20 00:00:00
REPLAY_SEED_SNAPSHOTS=1
SNAPSHOT_EVERY_SEC=300

# ---- TELEGRAM ----
TELEGRAM_API_ID=xxxx
TELEGRAM_API_HASH=yyyy
TELEGRAM_SESSION=nansen_reader
NANSEN_CHAT_NAME=NansenBot
BOT_TOKEN=zzz
CHANNEL_CHAT_ID=-100xxxx

# ---- TOKENS ----
TOKENS="AAVE,LINK,HYPE,ETH,STABLES"

# ---- UMBRALES ----
THRESHOLDS_JSON={"*":{"CEX":{"min_tx_usd":150000},"DEX":{"min_tx_usd":250000},"VC":{"min_tx_usd":1000000},"MERCADO":{"min_tx_usd":0}}}

# ---- AJUSTES DE CONFIANZA ----
CONF_SCALER_K_JSON={"*":0.20,"AAVE":0.40,"HYPE":0.45,"LINK":0.28,"ETH":0.22}
BASELINE_MODE=PCTL
BASELINE_PCTL=0.85
MARKET_CAP_USD={"AAVE":1200000000,"LINK":9000000000,"HYPE":200000000,"ETH":400000000000}

WEIGHTS_KIND_JSON={"CEX_IN":1.5,"CEX_OUT":-1.0,"DEX":0.5,"VC_IN":1.2,"VC_OUT":0.7,"MERCADO":0.3}

# ---- SALIDA ----
OUT_DIR=out
WRITE_SNAPSHOT=1
WRITE_HISTORY=1
```

---

## ▶️ Uso

1. Configura `.env` con tus credenciales y parámetros.
2. Ejecuta el listener:
   ```bash
   python nansen_listener1.py
   ```
3. Ejecuta el dashboard:
   ```bash
   streamlit run nansen_dashboard.py
   ```
4. Abre en navegador: [http://localhost:8501](http://localhost:8501)

---

## 🔍 Troubleshooting

- **`KeyError: ts_utc`**  
  El histórico tiene líneas corruptas/vacías. Revisa `snapshots_history.jsonl`.  

- **Graficas planas (conf=50)**  
  Significa que no se han parseado eventos. Ajusta `THRESHOLDS_JSON` o revisa el parser.

- **Demasiadas líneas en histórico**  
  Usa `SEED_HISTORY_LIMIT` para acotar.  

- **Canal no recibe mensajes**  
  Verifica `BOT_TOKEN` y `CHANNEL_CHAT_ID`.

---

## 📌 Próximos pasos / Ideas

- Ajustar `MARKET_CAP_USD` dinámicamente con API (CoinGecko).  
- Incluir métricas de *slippage estimado* en DEX.  
- Dashboard comparativo entre tokens.  
- Exportación de métricas a CSV/Excel para análisis offline.

# onchainspy
