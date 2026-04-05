# Municipal Domain Model Smoke QA

## Ordering Regression Smoke

### Direct pickup flow

**Customer**

Hola, soy Martín.

**Ruperto**

¡Hola, Martín! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¿Qué te gustaría pedir?

**Customer**

Quiero una hamburguesa doble cheddar para retirar.

**Ruperto**

Anotado, Martín: va 1 x Hamburguesa doble cheddar por $ 11.900. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

**Customer**

Pago en efectivo.

**Ruperto**

Así queda, Martín:

**Pedido**
- 1 x Hamburguesa doble cheddar

**Entrega**
Retiro por el local


**Pago**
Efectivo.

**Total**
$ 11.900

Si está bien así, confirmámelo y lo cierro.

**Customer**

Confirmá nomás.

**Ruperto**

Listo, Martín.

**Pedido**
- 1 x Hamburguesa doble cheddar

**Entrega**
Retiro por el local


**Pago**
Efectivo.

**Total**
$ 11.900

### Informational delivery question

**Customer**

Hola, ¿tenés para enviar?

**Ruperto**

Hacemos envíos. Para decirte si llegamos a tu zona necesito la dirección o al menos la referencia del barrio. Si me la pasás, lo revisamos antes de cerrar el pedido.

**Customer**

¿Y más o menos por qué zona llegan?

**Ruperto**

Hacemos envíos por la zona. Si querés, después te confirmo el alcance puntual con tu barrio o dirección antes de cerrar el pedido.

## Municipal Routing Smoke

**Customer**

Hola, quiero hacer un reclamo por una lámpara apagada.

**Municipal assistant**

Hola, soy el asistente municipal de Municipio Demo. La entrada multicanal ya está preparada para este vertical, pero la toma de reclamos y solicitudes todavía está en construcción. En el próximo paso vamos a habilitar áreas, categorías y seguimiento de casos.
