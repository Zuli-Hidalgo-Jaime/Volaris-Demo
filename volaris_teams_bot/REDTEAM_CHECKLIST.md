# Red Team Checklist (Flujo del Bot)

Objetivo: forzar errores de enrutamiento, perdida de contexto y bloqueos de follow-up para detectar mejoras.

## 1) Ambiguedad de dominio (politicas vs gastos)

Prueba cada prompt en conversacion nueva:

1. `Que gastos reconoce la politica de viaticos?`
2. `Registra un gasto de taxi por 220 en Orion`
3. `No me gaste todo el anticipo, que hago?`
4. `Cuanto tiempo tengo para comprobar un reporte?`

Que validar:

1. Preguntas de reglas/plazos => `politicas`
2. Acciones operativas => `gastos`
3. Consultas de anticipo => `politicas`

## 2) Intentos de romper con mezcla social + tarea

Secuencia:

1. `hola`
2. `gracias`
3. `ok, registra un gasto de hotel por 1800 en proyecto Atlas`
4. `y por 200 mas`

Que validar:

1. Saludos no deben bloquear el cambio a tarea.
2. El follow-up corto debe mantenerse en `gastos`.

## 3) Collab explicito vs implicito

Comparar:

1. `Que dice la politica de comprobantes y luego registra mi gasto de comida?`
2. `Que dice la politica de comprobantes?`
3. `Registra mi gasto de comida`

Que validar:

1. Caso 1 puede ir a `collab`.
2. Casos 2 y 3 no deben caer en `collab`.

## 4) Bloqueo por pending_route (stuck)

Secuencia:

1. Forzar respuesta del agente de gastos que pida confirmacion (`Responde exactamente: confirmo`).
2. En vez de confirmar, mandar: `cambiando de tema, que dice la politica de anticipos?`

Que validar:

1. Si queda atrapado en `pending_route=gastos`, anotar como bug.
2. Definir salida de escape (`cancelar`, `cambiar tema`, timeout, etc.).

## 5) Sticky route excesivo

Secuencia:

1. `Registra gasto taxi 300 proyecto Orion`
2. `cuales son los topes para hotel?`

Que validar:

1. El sticky no debe forzar `gastos` cuando ya hay pregunta clara de politica.

## 6) Robustez de parsing del router

Prueba respuestas de router con formato raro:

1. JSON limpio
2. JSON dentro de markdown
3. Texto sin JSON

Que validar:

1. Fallback consistente (sin crash).
2. Si no hay JSON valido, la inferencia local rescata ruta razonable.

## Ejecucion rapida automatizada

Puedes correr pruebas offline (sin Azure) con:

```powershell
python volaris_teams_bot/flow_breaker_tests.py
```

Estas pruebas validan heuristicas y guardrails actuales del flujo.
