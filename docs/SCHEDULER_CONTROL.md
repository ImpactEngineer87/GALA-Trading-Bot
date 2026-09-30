# Control manual y automático

El Dashboard mantiene dos modos de control del motor:

## Manual

- Es el modo predeterminado.
- El servidor `server.py` puede permanecer abierto.
- El operador enciende el motor con el botón superior.
- El operador puede apagarlo con el mismo botón.
- El cierre de seguridad configurado continúa liquidando y deteniendo el motor
  al final de la jornada.

## Automático — solo PAPER

- El servidor debe estar ejecutándose.
- A la hora de inicio (por defecto 09:15 ET), el motor se enciende para cargar
  datos y calentar indicadores.
- Las entradas continúan vetadas hasta 09:30 ET por el filtro RTH.
- A la hora final (por defecto 15:45 ET), se ejecuta el apagado normal, incluido
  el kill switch configurado.
- Si el operador lo apaga manualmente durante la sesión, el encendido automático
  queda suspendido hasta la siguiente jornada para que no se vuelva a encender
  cinco segundos después.

El horario usa siempre `America/New_York`, independientemente de la zona horaria
del VPS.

## Límite de seguridad

El modo automático no puede habilitarse desde el Dashboard cuando
`risk.paper_trading` es `false`. Si alguien combina manualmente AUTOMÁTICO +
REAL en `config.yaml`, el servidor no inicia el planificador. El dinero real
siempre exige encendido manual.

## Configuración

Desde `Configuración → Encendido y Apagado`:

1. Seleccione `Manual` o `Automático — solo PAPER`.
2. Configure las horas ET.
3. Pulse `Guardar Control Horario`.

Configuración equivalente:

```yaml
scheduler:
  modo: "manual"             # manual | automatico
  automatico_solo_paper: true
  hora_inicio: "09:15"
  hora_fin: "15:45"
  zona_horaria: "America/New_York"
  intervalo_revision_seg: 5
```
