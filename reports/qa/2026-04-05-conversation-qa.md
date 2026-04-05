# Conversational QA Report

- Generated: 2026-04-05 01:51 UTC
- Model: `gemini-2.5-flash`
- Scenarios: 25

## 1. direct_pickup_burger

- Persona: Cliente pragmático
- External user id: `qa-directo-1`
- Goal: Sabe exactamente lo que quiere y busca cerrar rápido.

### Turn 1

**Customer**

Hola, soy Martín.

**Ruperto**

¡Hola, Martín! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una hamburguesa doble cheddar para retirar.

**Ruperto**

Anotado, Martín: va 1 x Hamburguesa doble cheddar por $ 11.900. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 11.900. Ítems: 1 x Hamburguesa doble cheddar. Avisos automáticos: True.

### Turn 3

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

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 11.900. Ítems: 1 x Hamburguesa doble cheddar. Avisos automáticos: True.

### Turn 4

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

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 11.900. Ítems: 1 x Hamburguesa doble cheddar. Avisos automáticos: True.

---

## 2. indecisive_veggie

- Persona: Cliente indeciso
- External user id: `qa-indeciso-1`
- Goal: Duda entre varias opciones veggie y pide recomendación.

### Turn 1

**Customer**

Buenas noches

**Ruperto**

¡Buenas noches! 👋 ¡Mi Rotisería ya está abierta y listos para tomar tu pedido! 🚀 ¿Qué te gustaría cenar hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Algo veggie?

**Ruperto**

¡Sí, claro! Tenemos estas opciones veggies para vos: 

🍔 Hamburguesa veggie: Medallón de garbanzos, queso, rúcula, tomate y papas rústicas por $ 10.300.
🌯 Wrap veggie: Con vegetales salteados, hummus y hojas verdes por $ 8.800.

¿Cuál te tienta más? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Las dos, ¿cuál me recomendás? Tengo hambre.

**Ruperto**

Si tenés mucho hambre, te recomiendo la **Hamburguesa veggie** 🍔, ¡las papas rústicas siempre suman! 😉

¿Querés una de cada una, o te gustaría más de alguna en particular? Contame y las sumamos a tu pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 4

**Customer**

Dale, la burger.

**Ruperto**

Buenísimo, che: llevo 1 x Hamburguesa veggie por $ 10.300. Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 10.300. Ítems: 1 x Hamburguesa veggie. Avisos automáticos: True.

### Turn 5

**Customer**

¿Qué le puedo sumar?

**Ruperto**

¡Para acompañar tu hamburguesa, te puedo sugerir algo de beber o un postre! 🥤🍰

Algunas opciones son:

**Bebidas:**
* Gaseosa cola 1.5L por $ 3.200
* Cerveza rubia lata por $ 2.900

**Postres:**
* Flan casero por $ 3.500
* Brownie con nuez por $ 3.100

¿Algo te tienta? 😊

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 10.300. Ítems: 1 x Hamburguesa veggie. Avisos automáticos: True.

---

## 3. shipping_question_only

- Persona: Consulta informativa
- External user id: `qa-informativo-1`
- Goal: Todavía no quiere pedir; solo pregunta por envío.

### Turn 1

**Customer**

Hola, ¿tenés para enviar?

**Error**

`InformationalTurnMutationError: Este turno es solo informativo: no cambies el pedido todavía.`

---

## 4. soda_category_followup

- Persona: Cliente curioso
- External user id: `qa-bebidas-1`
- Goal: Pregunta por categoría y después por precio.

### Turn 1

**Customer**

No tenés gaseosas?

**Ruperto**

Sí, tenemos gaseosas. Por ejemplo:
- Gaseosa cola 1.5L: $ 3.200
- Gaseosa cola cero 1.5L: $ 3.300
- Gaseosa lima-limón lata: $ 2.200
- Gaseosa naranja lata: $ 2.200

Si querés, te sumo una al pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Cuánto sale?

**Ruperto**

Te detallo los precios de las gaseosas: 

🥤 Gaseosa cola 1.5L: $3.200
🥤 Gaseosa cola cero 1.5L: $3.300
🥤 Gaseosa lima-limón lata: $2.200
🥤 Gaseosa naranja lata: $2.200

¿Querés agregar alguna? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 5. scheduled_lunch_order

- Persona: Cliente organizado
- External user id: `qa-programado-1`
- Goal: Quiere un pedido programado para una hora concreta.

### Turn 1

**Customer**

Soy Pedro

**Ruperto**

¡Hola Pedro! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Una hamburguesa doble cheddar y una cerveza rubia. ¿Me lo preparás para las 12?

**Ruperto**

Puedo programarlo solo dentro del horario del local. El próximo horario disponible es mañana a las 19:00.

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 14.800. Ítems: 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata. Avisos automáticos: True.

### Turn 3

**Customer**

Lo retiro.

**Ruperto**

Perfecto. El total es $ 14.800. ¿Cómo querés pagar: efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 14.800. Ítems: 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata. Avisos automáticos: True.

### Turn 4

**Customer**

Transferencia.

**Ruperto**

Así queda, Pedro:

**Pedido**
- 1 x Hamburguesa doble cheddar
- 1 x Cerveza rubia lata

**Entrega**
Retiro por el local


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 14.800

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 14.800. Ítems: 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata. Avisos automáticos: True.

### Turn 5

**Customer**

Confirmá el pedido.

**Ruperto**

Listo, Pedro.

**Pedido**
- 1 x Hamburguesa doble cheddar
- 1 x Cerveza rubia lata

**Entrega**
Retiro por el local


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 14.800

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 14.800. Ítems: 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata. Avisos automáticos: True.

---

## 6. lomito_correction

- Persona: Cliente que corrige
- External user id: `qa-correccion-1`
- Goal: Hace un pedido ambiguo y luego lo corrige.

### Turn 1

**Customer**

Hola, soy Ana.

**Ruperto**

¡Hola, Ana! 👋 ¿Cómo estás? Contame qué te gustaría pedir hoy de Mi Rotisería. 🍗

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

2 lomos y unas papas.

**Ruperto**

Ana, de "papas" tenemos estas opciones:

*   **Papas fritas clásicas** a $4.300 🍟
*   **Papas cheddar y bacon** a $6.900 🥓

Con respecto a los lomos, no los encuentro en el menú. ¿Quizás quisiste decir otra cosa o preferís ver otras opciones de plato principal? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Uno y uno.

**Ruperto**

Anotado, Ana: hasta ahora va 1 x Papas fritas clásicas, 1 x Papas cheddar y bacon por $ 11.200. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 11.200. Ítems: 1 x Papas fritas clásicas, 1 x Papas cheddar y bacon. Avisos automáticos: True.

### Turn 4

**Customer**

Dije uno de cada uno.

**Ruperto**

Anotado, Ana: hasta ahora va 1 x Papas fritas clásicas, 1 x Papas cheddar y bacon por $ 11.200. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 11.200. Ítems: 1 x Papas fritas clásicas, 1 x Papas cheddar y bacon. Avisos automáticos: True.

---

## 7. unsupported_customization

- Persona: Cliente especial
- External user id: `qa-especial-1`
- Goal: Pide una personalización inexistente para ver cómo responde el bot.

### Turn 1

**Customer**

Hola, soy Nico.

**Ruperto**

¡Hola, Nico! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero la hamburguesa picante pero con doble picante y triple cheddar.

**Ruperto**

Buenísimo, Nico: llevo 1 x Hamburguesa picante por $ 12.100. Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 12.100. Ítems: 1 x Hamburguesa picante. Avisos automáticos: True.

### Turn 3

**Customer**

¿Eso se puede?

**Ruperto**

Bien, Nico: tengo 1 x Hamburguesa picante por $ 12.100. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 12.100. Ítems: 1 x Hamburguesa picante. Avisos automáticos: True.

---

## 8. off_topic_request

- Persona: Fuera de dominio
- External user id: `qa-fuera-dominio-1`
- Goal: Pregunta algo que no tiene nada que ver con la rotisería.

### Turn 1

**Customer**

Hola, ¿me podés decir quién ganó el mundial 2022?

**Ruperto**

¡Hola! Soy Ruperto, el asistente de Mi Rotisería, y mi función es ayudarte con los pedidos de comida. 😊 No tengo información sobre eventos deportivos como el Mundial. ¿Te puedo ayudar con algo del menú o con tu pedido?

- Next step: `handoff`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Y me hacés un resumen del partido?

**Ruperto**

Disculpá, sigo sin poder ayudarte con información del Mundial. Mi especialidad es la comida rica de Mi Rotisería. 😋 ¿Te gustaría ver nuestro menú o hacer un pedido?

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 9. main_plus_side_upsell

- Persona: Cliente con hambre
- External user id: `qa-upsell-1`
- Goal: Pide principal y guarnición para ver si todavía propone bebida o postre.

### Turn 1

**Customer**

Hola, soy Juli.

**Ruperto**

¡Hola Juli! 👋 Estamos abiertos ahora mismo 🍽️. ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

1 sanguche de mila y unas papas clásicas.

**Ruperto**

Dale, Juli: tengo 1 x Sanguche de milanesa, 1 x Papas fritas clásicas por $ 13.200. Si querés, podés sumar una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 13.200. Ítems: 1 x Sanguche de milanesa, 1 x Papas fritas clásicas. Avisos automáticos: True.

---

## 10. delivery_cost_question

- Persona: Cliente cuidadoso
- External user id: `qa-envio-costo-1`
- Goal: Pregunta por costo de envío después de armar pedido parcial.

### Turn 1

**Customer**

Hola, soy Lucas.

**Ruperto**

¡Hola, Lucas! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una hamburguesa completa.

**Ruperto**

¿Querés la Hamburguesa completa que viene con queso, lechuga, tomate y papas fritas por $9.500? 🍔

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Envío.

**Ruperto**

¿A qué dirección te lo enviamos?

- Next step: `ask_address`
- Order: Estado: draft. Total: $ 0. Ítems: —. Avisos automáticos: True.

### Turn 4

**Customer**

¿El envío tiene costo?

**Ruperto**

Disculpá, no tengo esa información en este momento. Para poder decirte el costo de envío, necesito que me pases la dirección.

- Next step: `ask_address`
- Order: Sin pedido activo.

---

## 11. known_customer_repeat

- Persona: Cliente recurrente
- External user id: `qa-recurrente-1`
- Goal: Hace un pedido y luego vuelve a escribir para ver si conserva contexto.

### Turn 1

**Customer**

Hola, soy Elena.

**Ruperto**

¡Hola, Elena! 😊 Qué bueno verte por acá. Estamos abiertos ahora 🍽️ hasta las 23:00. ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una pizza napolitana.

**Ruperto**

Buenísimo, Elena: llevo 1 x Pizza napolitana por $ 12.400. Si querés completar el pedido, podés sumar una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 12.400. Ítems: 1 x Pizza napolitana. Avisos automáticos: True.

### Turn 3

**Customer**

Retiro.

**Ruperto**

Ya tengo $ 12.400. ¿Lo resolvemos con efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 12.400. Ítems: 1 x Pizza napolitana. Avisos automáticos: True.

### Turn 4

**Customer**

Pago efectivo.

**Ruperto**

Así queda, Elena:

**Pedido**
- 1 x Pizza napolitana

**Entrega**
Retiro por el local


**Pago**
Efectivo.

**Total**
$ 12.400

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 12.400. Ítems: 1 x Pizza napolitana. Avisos automáticos: True.

### Turn 5

**Customer**

Confirmá.

**Ruperto**

Listo, Elena.

**Pedido**
- 1 x Pizza napolitana

**Entrega**
Retiro por el local


**Pago**
Efectivo.

**Total**
$ 12.400

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 12.400. Ítems: 1 x Pizza napolitana. Avisos automáticos: True.

### Turn 6

**Customer**

Che, ¿te acordás qué pedí recién?

**Ruperto**

¡Claro! Recién pediste una Pizza Napolitana. 🍕

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 12.400. Ítems: 1 x Pizza napolitana. Avisos automáticos: True.

---

## 12. price_before_name

- Persona: Cliente directo
- External user id: `qa-precio-antes-nombre-1`
- Goal: Pregunta precios sin presentarse.

### Turn 1

**Customer**

¿Cuánto sale una fugazzeta?

**Ruperto**

La Pizza fugazzeta sale $ 12.800. 🍕

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Y una cerveza IPA, ¿cuánto?

**Ruperto**

La Cerveza IPA lata cuesta $ 3.400. 🍻

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 13. post_order_notification_expectation

- Persona: Cliente prevenido
- External user id: `qa-notificacion-1`
- Goal: Quiere saber si le avisan cuando esté listo.

### Turn 1

**Customer**

Hola, soy Carla.

**Ruperto**

¡Hola, Carla! 👋 ¿Cómo estás? ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero un lomito especial.

**Ruperto**

Dale, Carla: tengo 1 x Lomito especial por $ 14.200. Si querés, podés sumar unas papas, una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 14.200. Ítems: 1 x Lomito especial. Avisos automáticos: True.

### Turn 3

**Customer**

Retiro.

**Ruperto**

Perfecto. El total es $ 14.200. ¿Cómo querés pagar: efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 14.200. Ítems: 1 x Lomito especial. Avisos automáticos: True.

### Turn 4

**Customer**

Pago con transferencia.

**Ruperto**

Así queda, Carla:

**Pedido**
- 1 x Lomito especial

**Entrega**
Retiro por el local


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 14.200

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 14.200. Ítems: 1 x Lomito especial. Avisos automáticos: True.

### Turn 5

**Customer**

¿Me avisás cuando esté listo?

**Ruperto**

Listo, Carla.

**Pedido**
- 1 x Lomito especial

**Entrega**
Retiro por el local


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 14.200

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 14.200. Ítems: 1 x Lomito especial. Avisos automáticos: True.

---

## 14. large_family_order

- Persona: Pedido grande
- External user id: `qa-grande-1`
- Goal: Hace un pedido largo con varias líneas y cantidades.

### Turn 1

**Customer**

Hola, soy Diego.

**Ruperto**

¡Hola, Diego! 👋 ¿Cómo puedo ayudarte hoy con tu pedido?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero 2 pizzas muzza, 1 docena de empanadas clásicas y 2 gaseosas cola 1.5L.

**Ruperto**

Anotado, Diego: va 2 x Pizza muzzarella, 1 x Docena de empanadas clásicas, 2 x Gaseosa cola 1.5L por $ 48.200. Si te copa, le podés agregar un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 48.200. Ítems: 2 x Pizza muzzarella, 1 x Docena de empanadas clásicas, 2 x Gaseosa cola 1.5L. Avisos automáticos: True.

### Turn 3

**Customer**

Envío.

**Ruperto**

Dale. Pasame la dirección de envío, por favor.

- Next step: `ask_address`
- Order: Estado: draft. Total: $ 48.200. Ítems: 2 x Pizza muzzarella, 1 x Docena de empanadas clásicas, 2 x Gaseosa cola 1.5L. Avisos automáticos: True.

### Turn 4

**Customer**

A 9 de Julio 1302, Anisacate.

**Ruperto**

Son $ 48.200. ¿Preferís efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 48.200. Ítems: 2 x Pizza muzzarella, 1 x Docena de empanadas clásicas, 2 x Gaseosa cola 1.5L. Avisos automáticos: True.

### Turn 5

**Customer**

Link de pago.

**Ruperto**

Así queda, Diego:

**Pedido**
- 2 x Pizza muzzarella
- 1 x Docena de empanadas clásicas
- 2 x Gaseosa cola 1.5L

**Entrega**
Envío a 9 de Julio 1302, Anisacate


**Pago**
Link de pago.

**Total**
$ 48.200

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 48.200. Ítems: 2 x Pizza muzzarella, 1 x Docena de empanadas clásicas, 2 x Gaseosa cola 1.5L. Avisos automáticos: True.

---

## 15. dessert_only

- Persona: Antojo nocturno
- External user id: `qa-postre-1`
- Goal: Pide solo postres y bebidas, sin plato principal.

### Turn 1

**Customer**

Hola, soy Belu.

**Ruperto**

¡Hola, Belu! 👋 ¿En qué puedo ayudarte hoy? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Qué postres tienen?

**Ruperto**

Sí, tenemos postres. Por ejemplo:
- Brownie con nuez: $ 3.100
- Budín de pan: $ 2.900
- Cheesecake de frutos rojos: $ 5.400
- Flan casero: $ 3.500

Si querés, te sumo una al pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Dame un tiramisú y un cheesecake.

**Ruperto**

Dale, Belu: por ahora llevo 1 x Cheesecake de frutos rojos por $ 5.400. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 5.400. Ítems: 1 x Cheesecake de frutos rojos. Avisos automáticos: True.

### Turn 4

**Customer**

¿Tenés algo para tomar sin alcohol?

**Ruperto**

Sí, tenemos bebidas. Por ejemplo:
- Agua con gas 500ml: $ 1.900
- Agua saborizada pomelo 1.5L: $ 2.800
- Agua sin gas 500ml: $ 1.800
- Gaseosa cola 1.5L: $ 3.200

Si querés, te sumo una al pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 16. address_reuse

- Persona: Cliente repetido
- External user id: `qa-direccion-1`
- Goal: Primero deja dirección, luego vuelve a pedir para ver si se reutiliza.

### Turn 1

**Customer**

Hola, soy Pablo.

**Ruperto**

¡Hola, Pablo! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una milanesa napolitana.

**Ruperto**

Anotado, Pablo: va 1 x Milanesa napolitana por $ 12.500. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 12.500. Ítems: 1 x Milanesa napolitana. Avisos automáticos: True.

### Turn 3

**Customer**

Envío.

**Ruperto**

Dale. Pasame la dirección de envío, por favor.

- Next step: `ask_address`
- Order: Estado: draft. Total: $ 12.500. Ítems: 1 x Milanesa napolitana. Avisos automáticos: True.

### Turn 4

**Customer**

Lavalle 12333.

**Ruperto**

Son $ 12.500. ¿Preferís efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 12.500. Ítems: 1 x Milanesa napolitana. Avisos automáticos: True.

### Turn 5

**Customer**

Efectivo.

**Ruperto**

Así queda, Pablo:

**Pedido**
- 1 x Milanesa napolitana

**Entrega**
Envío a Lavalle 12333


**Pago**
Efectivo.

**Total**
$ 12.500

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 12.500. Ítems: 1 x Milanesa napolitana. Avisos automáticos: True.

### Turn 6

**Customer**

Confirmá.

**Ruperto**

Listo, Pablo.

**Pedido**
- 1 x Milanesa napolitana

**Entrega**
Envío a Lavalle 12333


**Pago**
Efectivo.

**Total**
$ 12.500

- Next step: `complete`
- Order: Estado: confirmed. Total: $ 12.500. Ítems: 1 x Milanesa napolitana. Avisos automáticos: True.

### Turn 7

**Customer**

Ahora quiero una hamburguesa BBQ.

**Ruperto**

Buenísimo, Pablo: llevo 1 x Hamburguesa BBQ por $ 11.500. Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

### Turn 8

**Customer**

Envío.

**Ruperto**

Listo. ¿Te sirve mandarlo a Lavalle 12333? Si no, pasame la dirección correcta.

- Next step: `ask_address`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

---

## 17. ambiguous_beer_choice

- Persona: Cliente coloquial
- External user id: `qa-cerveza-1`
- Goal: Usa expresiones ambiguas al sumar bebida.

### Turn 1

**Customer**

Soy Fer.

**Ruperto**

¡Hola, Fer! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una burger veggie.

**Ruperto**

Disculpá, Fer, no encuentro la burger veggie en el menú. ¿Querés que busquemos otra cosa o te muestro las opciones de burgers que tenemos?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Sumame una cervecita.

**Ruperto**

¡Claro! Tenemos estas opciones de cerveza:

🍺 Cerveza IPA lata: $3.400
🍺 Cerveza roja lata: $3.100
🍺 Cerveza rubia lata: $2.900

¿Cuál te gustaría?

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 18. menu_browse_then_order

- Persona: Cliente explorador
- External user id: `qa-carta-1`
- Goal: Pide la carta, recorre y recién después decide.

### Turn 1

**Customer**

Hola, ¿me pasás la carta?

**Ruperto**

¡Hola! 👋 Te paso nuestra carta para que veas todas las opciones que tenemos:

**COMIDAS** 🍽️
*   Empanadas de carne: $ 1.800
*   Empanadas de pollo: $ 1.800
*   Empanadas de jamón y queso: $ 1.800
*   Empanadas de verdura: $ 1.750
*   Docena de empanadas clásicas: $ 19.800
*   Hamburguesa clásica: $ 7.900
*   Hamburguesa completa: $ 9.500
*   Hamburguesa doble cheddar: $ 11.900
*   Hamburguesa BBQ: $ 11.500
*   Hamburguesa picante: $ 12.100
*   Hamburguesa veggie: $ 10.300
*   Combo hamburguesa completa (con papas grandes y gaseosa lata): $ 13.800
*   Lomito completo: $ 13.200
*   Lomito especial: $ 14.200
*   Milanesa napolitana: $ 12.500
*   Milanesa completa: $ 11.900
*   Milanesa a caballo: $ 12.800
*   Sanguche de milanesa: $ 8.900
*   Pizza muzzarella: $ 11.000
*   Pizza napolitana: $ 12.400
*   Pizza fugazzeta: $ 12.800
*   Pizza especial: $ 13.900
*   Pizza calabresa: $ 14.800
*   Pizza cuatro quesos: $ 15.400
*   Pizza jamón y morrón: $ 14.400
*   Pizza rúcula y crudo: $ 16.800
*   Wrap de pollo: $ 9.200
*   Wrap veggie: $ 8.800
*   Ensalada César: $ 8.700
*   Papas fritas clásicas: $ 4.300
*   Papas cheddar y bacon: $ 6.900

**BEBIDAS** 🥤
*   Agua sin gas 500ml: $ 1.800
*   Agua con gas 500ml: $ 1.900
*   Agua saborizada pomelo 1.5L: $ 2.800
*   Gaseosa lima-limón lata: $ 2.200
*   Gaseosa naranja lata: $ 2.200
*   Gaseosa cola 1.5L: $ 3.200
*   Gaseosa cola cero 1.5L: $ 3.300
*   Cerveza rubia lata: $ 2.900
*   Cerveza IPA lata: $ 3.400
*   Cerveza roja lata: $ 3.100

**POSTRES** 🍰
*   Flan casero: $ 3.500
*   Tiramisú: $ 5.200
*   Helado 1/4 kg: $ 4.200
*   Helado 1/2 kg: $ 7.600
*   Brownie con nuez: $ 3.100
*   Cheesecake de frutos rojos: $ 5.400
*   Budín de pan: $ 2.900

¿Qué te gustaría pedir? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Qué wraps hay?

**Ruperto**

¡Claro! Tenemos dos opciones de wraps para vos:

*   Wrap de pollo: $ 9.200
*   Wrap veggie: $ 8.800

¿Te gustaría probar alguno? 🌯

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Bueno, dame el wrap de pollo.

**Ruperto**

Dale, che: tengo 1 x Wrap de pollo por $ 9.200. Si querés, podés sumar unas papas, una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 9.200. Ítems: 1 x Wrap de pollo. Avisos automáticos: True.

### Turn 4

**Customer**

Retiro.

**Ruperto**

Perfecto. El total es $ 9.200. ¿Cómo querés pagar: efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 9.200. Ítems: 1 x Wrap de pollo. Avisos automáticos: True.

---

## 19. wrong_channel_expectation

- Persona: Cliente techie
- External user id: `qa-canal-1`
- Goal: Pregunta por canales y cosas fuera de operación.

### Turn 1

**Customer**

¿Tenés Instagram o solo WhatsApp?

**Ruperto**

¡Hola! Por ahora solo gestionamos pedidos por WhatsApp. 😊 Si necesitás ayuda con algo más, avisame.

- Next step: `handoff`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Puedo pagar con crypto?

**Ruperto**

¡Hola! Por el momento no aceptamos pagos con criptomonedas. Podés pagar en efectivo, con tarjeta (link de pago) o transferencia. ¿Cuál preferís? 💳💸

- Next step: `choose_payment`
- Order: Sin pedido activo.

---

## 20. allergic_customer

- Persona: Cliente con restricción
- External user id: `qa-alergia-1`
- Goal: Consulta por ingredientes y modificaciones.

### Turn 1

**Customer**

Hola, soy Mica.

**Ruperto**

¡Hola, Mica! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿La ensalada César trae pollo sí o sí?

**Ruperto**

Sí, Mica. La Ensalada César trae pollo grillado. 🥗 ¿Te gustaría pedir una o tenés alguna otra consulta?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

¿Y la hamburguesa veggie tiene queso?

**Ruperto**

Sí, Mica. La Hamburguesa Veggie lleva queso 🧀. También trae medallón de garbanzos, rúcula, tomate y papas rústicas. ¿Te gustaría pedirla o preferís ver otras opciones?

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 21. natural_language_combo

- Persona: Cliente apurado
- External user id: `qa-natural-1`
- Goal: Mete todo junto en una sola frase.

### Turn 1

**Customer**

Hola soy Tomi quiero una pizza especial para enviar a Olegario Andrade 330 y pago con transferencia

**Ruperto**

Así queda, Tomi:

**Pedido**
- 1 x Pizza especial

**Entrega**
Envío a Olegario Andrade 330


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 13.900

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 13.900. Ítems: 1 x Pizza especial. Avisos automáticos: True.

---

## 22. cash_mixed_with_english

- Persona: Cliente code-switch
- External user id: `qa-cash-english-1`
- Goal: Mezcla español e inglés en el pago.

### Turn 1

**Customer**

Hola, soy Juani.

**Ruperto**

¡Hola Juani! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Una hamburguesa BBQ.

**Ruperto**

Buenísimo, Juani: llevo 1 x Hamburguesa BBQ por $ 11.500. Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

### Turn 3

**Customer**

Retiro.

**Ruperto**

Ya tengo $ 11.500. ¿Lo resolvemos con efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

### Turn 4

**Customer**

Pago en el local, cash.

**Ruperto**

Así queda, Juani:

**Pedido**
- 1 x Hamburguesa BBQ

**Entrega**
Retiro por el local


**Pago**
Efectivo.

**Total**
$ 11.500

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

---

## 23. rejection_of_upsell

- Persona: Cliente seco
- External user id: `qa-no-upsell-1`
- Goal: Rechaza explícitamente cualquier agregado.

### Turn 1

**Customer**

Hola, soy Agus.

**Ruperto**

¡Hola, Agus! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una pizza rúcula y crudo.

**Ruperto**

Dale, Agus: tengo 1 x Pizza rúcula y crudo por $ 16.800. Si querés, podés sumar una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 16.800. Ítems: 1 x Pizza rúcula y crudo. Avisos automáticos: True.

### Turn 3

**Customer**

Nada más.

**Ruperto**

Dale, Agus: por ahora llevo 1 x Pizza rúcula y crudo por $ 16.800. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 16.800. Ítems: 1 x Pizza rúcula y crudo. Avisos automáticos: True.

### Turn 4

**Customer**

Retiro.

**Error**

`MissingConfirmationSignalError: No confirmes el pedido sin una señal explícita del cliente en este turno.`

---

## 24. exact_price_comparison

- Persona: Cliente comparador
- External user id: `qa-comparador-1`
- Goal: Quiere comparar dos productos antes de decidir.

### Turn 1

**Customer**

¿Qué sale más, el lomito completo o la milanesa completa?

**Ruperto**

👋 Hola, soy Ruperto, el asistente de pedidos de Mi Rotisería. Antes de seguir, ¿me decís tu nombre?

- Next step: `ask_name`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Y cuál te parece más llenador?

**Ruperto**

🙂 Necesito tu nombre para seguir con lo que me pediste. ¿Cómo te llamás?

- Next step: `ask_name`
- Order: Sin pedido activo.

---

## 25. abandoned_then_resume

- Persona: Cliente intermitente
- External user id: `qa-retoma-1`
- Goal: Deja la charla a medias y vuelve después dentro de la misma conversación.

### Turn 1

**Customer**

Hola, soy Emi.

**Ruperto**

¡Hola Emi! 👋 ¿Cómo puedo ayudarte hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una hamburguesa completa.

**Ruperto**

Anotado, Emi: va 1 x Hamburguesa completa por $ 9.500. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

### Turn 3

**Customer**

Pará, todavía no sé si la voy a retirar.

**Ruperto**

Anotado, Emi: hasta ahora va 1 x Hamburguesa completa por $ 9.500. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

### Turn 4

**Customer**

Bueno, sí, retiro.

**Ruperto**

Son $ 9.500. ¿Preferís efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

### Turn 5

**Customer**

Transferencia.

**Ruperto**

Así queda, Emi:

**Pedido**
- 1 x Hamburguesa completa

**Entrega**
Retiro por el local


**Pago**
Transferencia a `demo.rotiseria`.

**Total**
$ 9.500

Si está bien así, confirmámelo y lo cierro.

- Next step: `confirm_order`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

---
