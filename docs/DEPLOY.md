# Despliegue en el VPS — GALA10 Chocoletra Bot

Guía para desplegar la versión validada en un VPS Windows. Trabaje por RDP y
no publique credenciales, direcciones ni el puerto de Flask en documentación.

> IMPORTANTE: el paquete de despliegue **NO incluye el archivo `.env`**, para
> no sobrescribir las credenciales/tokens de Schwab que ya funcionan en el VPS.
> Al extraer encima de la carpeta actual, tu `.env` se conserva intacto.

---

## 1. Copia de seguridad (1 min)
En el VPS, duplica la carpeta actual del bot por si acaso:
`chocoletra_bot`  →  `chocoletra_bot_backup`

## 2. Subir el código nuevo
- Sube el paquete de código validado al VPS.
- Extráelo **encima** de la carpeta `chocoletra_bot` (reemplaza los archivos de
  código). Tu `.env` no se toca porque no viene en el zip.

## 3. Instalar dependencias
Abre PowerShell o CMD dentro de `chocoletra_bot` y ejecuta:
```
pip install -r requirements.txt
```
(No hay dependencias nuevas de pago; solo se reutilizan xgboost/scikit-learn.)

## 4. Pre-entrenar la IA con el historial (arranque en caliente)
Para que el bot NO empiece de cero:
```
python PRE_ENTRENAR.py
```
Esto reproduce las señales GALA10 con velas cerradas de 1 minuto, volumen y
liquidez, y evalúa modelos por ticker. Revise
`data/reportes/metricas_modelos_latest.csv`. Un modelo que no supera las
puertas de validación debe permanecer desactivado.

No ejecute los módulos heredados de `ai_filter`; no corresponden al flujo
GALA10 + agentes de veto validado.

## 5. Configurar y arrancar el Dashboard

Configure un usuario, una contraseña de al menos 12 caracteres y una clave de
sesión aleatoria en `.env`. Las contraseñas nuevas guardadas desde el panel se
convierten a `DASHBOARD_PASSWORD_HASH` y no se conservan en texto plano.

```
python server.py
```
o `START_BOT.bat`. Abre en el navegador:
`http://127.0.0.1:5000`

Verifica en el Dashboard:
- Schwab conectado (pestaña Config → estado).
- Tickers cargados.
- Pulsa **ENCENDER**; deberías ver el ciclo y las señales/dirección del día.

### Control horario

En `Configuración → Encendido y Apagado` puede seleccionar:

- `Manual`: el operador pulsa el botón de encendido.
- `Automático — solo PAPER`: el motor arranca a las 09:15 ET para calentamiento
  y se apaga/liquida a las 15:45 ET. Las entradas siguen vetadas hasta 09:30 ET.

`server.py` debe permanecer ejecutándose para que el planificador funcione. El
modo automático nunca habilita dinero real. Ver `docs/SCHEDULER_CONTROL.md`.

## 6. Preflight Schwab de solo lectura

Antes de habilitar órdenes:

```powershell
python main.py --modo broker-check --account ACCOUNT_NUMBER --tickers MSFT
```

Continúe únicamente si el resultado es `APROBADO`.

## 7. Medición Schwab de solo lectura

Después del preflight, genere un reporte de latencia con la sesión HTTP ya
caliente:

```powershell
python main.py --modo broker-latency --account ACCOUNT_NUMBER --tickers MSFT --samples 10
```

El resultado queda en `data/reportes/broker_latency_latest.json`. Esta prueba
no envía órdenes y no sustituye la validación de fills durante mercado abierto.

## 8. Validación supervisada en REAL

Esta versión no debe habilitarse en real solo por haber superado tests
automáticos. Primero deben aprobarse la sesión paper durante horario regular,
las métricas de IA y el plan de prueba con el titular de la cuenta.

Para validar el reparto 70/30 hacen falta al menos 3 acciones. La cantidad, el
ticker y el riesgo deben autorizarse expresamente antes de la sesión. El CLI
exige un segundo preflight y una frase ligada a los últimos cuatro dígitos de
la cuenta.

## 9. Escalar
Cuando varias operaciones entren/salgan limpias, sube el tamaño:
- `max_acciones_por_orden: 0` (sin límite → tamaño por gestión de riesgo), o
- un número mayor (ej. 5, 10) para subir de forma gradual.

---

## Recordatorios de seguridad
- La autenticación del Dashboard debe permanecer activa.
- No abra públicamente el puerto 5000. Use RDP o un proxy HTTPS con firewall.
- El botón APAGAR ejecuta la secuencia configurada de kill switch; confirme el
  estado final directamente en Schwab.
- `STOP_BOT.bat` es solo para una emergencia técnica: finaliza el proceso sin
  poder confirmar cancelaciones ni cierres en el broker.
- Ver `docs/handover.md` para la arquitectura completa.
