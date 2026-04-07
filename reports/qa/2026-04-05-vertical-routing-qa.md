# Conversational QA Report

- Generated: 2026-04-05 20:39 UTC
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

¡Hola, Martín! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. Igual, podés ir viendo nuestro menú y armar tu pedido. ¿Qué se te antoja hoy?

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

Anotado, Martín: hasta ahora va 1 x Hamburguesa doble cheddar por $ 11.900. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 11.900. Ítems: 1 x Hamburguesa doble cheddar. Avisos automáticos: True.

### Turn 4

**Customer**

Confirmá nomás.

**Ruperto**

Anotado, Martín: hasta ahora va 1 x Hamburguesa doble cheddar por $ 11.900. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 11.900. Ítems: 1 x Hamburguesa doble cheddar. Avisos automáticos: True.

---

## 2. indecisive_veggie

- Persona: Cliente indeciso
- External user id: `qa-indeciso-1`
- Goal: Duda entre varias opciones veggie y pide recomendación.

### Turn 1

**Customer**

Buenas noches

**Ruperto**

¡Buenas noches! Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¿Querés ver nuestro menú o hacer un pedido para más tarde?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Algo veggie?

**Ruperto**

¡Claro! Tenemos estas opciones veggies para vos:

🍔 Hamburguesa veggie: Medallón de garbanzos, queso, rúcula, tomate y papas rústicas por $10.300.
🌯 Wrap veggie: Con vegetales salteados, hummus y hojas verdes por $8.800.

¿Te gustaría sumar alguna para cuando abramos hoy a las 19:00? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Las dos, ¿cuál me recomendás? Tengo hambre.

**Ruperto**

Dale, che: tengo 1 x Wrap veggie, 1 x Hamburguesa veggie por $ 19.100. Si querés, podés sumar unas papas, una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 19.100. Ítems: 1 x Wrap veggie, 1 x Hamburguesa veggie. Avisos automáticos: True.

### Turn 4

**Customer**

Dale, la burger.

**Ruperto**

Dale, che: tengo 1 x Hamburguesa veggie por $ 10.300. Si querés, podés sumar unas papas, una bebida o un postre. ¿Te tienta algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 10.300. Ítems: 1 x Hamburguesa veggie. Avisos automáticos: True.

### Turn 5

**Customer**

¿Qué le puedo sumar?

**Ruperto**

Para acompañar tu Hamburguesa veggie 🍔, ¿qué te parece una Gaseosa cola 1.5L por $3.200, o quizás un Budín de pan 🍮 por $2.900 de postre? ¡También tenemos otras bebidas y postres! 😉

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

**Ruperto**

Hacemos envíos. Para decirte si llegamos a tu zona necesito la dirección o al menos la referencia del barrio. Si me la pasás, lo revisamos antes de cerrar el pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Y más o menos por qué zona llegan?

**Ruperto**

Hacemos envíos por la zona. Si querés, después te confirmo el alcance puntual con tu barrio o dirección antes de cerrar el pedido.

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 4. soda_category_followup

- Persona: Cliente curioso
- External user id: `qa-bebidas-1`
- Goal: Pregunta por categoría y después por precio.

### Turn 1

**Customer**

No tenés gaseosas?

**Ruperto**

Justo ahora el local está cerrado 😴 Abrimos hoy a las 19:00. Sí, tenemos gaseosas. Por ejemplo:
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

*   **Gaseosa cola 1.5L:** $3.200
*   **Gaseosa cola cero 1.5L:** $3.300
*   **Gaseosa lima-limón lata:** $2.200
*   **Gaseosa naranja lata:** $2.200

¿Hay alguna que quieras sumar? 😊

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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola, Pedro! 👋 ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Una hamburguesa doble cheddar y una cerveza rubia. ¿Me lo preparás para las 12?

**Ruperto**

Anotado, Pedro: va 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata por $ 14.800. Lo dejo programado para mañana a las 12:00. Si te copa, le podés agregar un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 14.800. Ítems: 1 x Hamburguesa doble cheddar, 1 x Cerveza rubia lata. Avisos automáticos: True.

### Turn 3

**Customer**

Lo retiro.

**Ruperto**

Son $ 14.800. Lo dejo programado para mañana a las 12:00. ¿Preferís efectivo, transferencia o link de pago?

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

**Horario**
Pedido programado para mañana a las 12:00.

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

**Horario**
Pedido programado para mañana a las 12:00.
Empezamos a prepararlo cerca de las 11:39.

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

¡Hola, Ana! 👋 Soy Ruperto, tu asistente virtual de Mi Rotisería. Ahora mismo estamos cerrados 😴 Abrimos hoy a las 19:00 hs. ¿Querés ir viendo nuestro menú para cuando abramos?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

2 lomos y unas papas.

**Ruperto**

¡Genial, Ana! Por "lomos" no encontré nada exacto en el menú, ¿será algún sándwich de lomo o plato en particular? 🤔

Para las papas, tenemos estas opciones:
🍟 Papas fritas clásicas: $ 4.300
🧀 Papas cheddar y bacon: $ 6.900

Recordá que estamos cerrados, abrimos hoy a las 19:00 hs. Podés ir armando tu pedido para esa hora. 😉

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

Uno y uno.

**Ruperto**

Para que no haya errores, ¿me dirías qué tipo de lomo te gustaría y cuál opción de papas preferís? Así te armo el pedido exacto. 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 4

**Customer**

Dije uno de cada uno.

**Ruperto**

Disculpame, Ana. Para poder ayudarte, necesito que me digas qué tipo de lomo te gustaría, ya que no encontré ninguno con ese nombre en nuestro menú. Y de las papas, ¿cuáles preferís: las Papas fritas clásicas o las Papas cheddar y bacon? Así armamos tu pedido. 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

---

## 7. unsupported_customization

- Persona: Cliente especial
- External user id: `qa-especial-1`
- Goal: Pide una personalización inexistente para ver cómo responde el bot.

### Turn 1

**Customer**

Hola, soy Nico.

**Ruperto**

¡Hola, Nico! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¿Qué te gustaría pedir para más tarde?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero la hamburguesa picante pero con doble picante y triple cheddar.

**Ruperto**

Buenísimo, Nico: llevo 1 x Hamburguesa picante por $ 12.100.  Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 12.100. Ítems: 1 x Hamburguesa picante. Avisos automáticos: True.

### Turn 3

**Customer**

¿Eso se puede?

**Ruperto**

¡Buena pregunta, Nico! Esa es una personalización que le pasamos directo a la cocina. Ellos lo preparan si es posible, y si hubiera algún problema o cambio te avisamos al instante. 😉

- Next step: `choose_items`
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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola! Soy Ruperto, el asistente de Mi Rotisería, y mi especialidad es ayudarte con los pedidos de comida. 😊 No tengo información sobre eventos deportivos. ¿Te gustaría ver nuestro menú o hacer un pedido?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Y me hacés un resumen del partido?

**Ruperto**

Disculpá, pero no tengo acceso a información de eventos deportivos. Soy Ruperto, tu asistente de Mi Rotisería, ¡listo para tomar tu pedido! 🍔 ¿Hay algo del menú que te interese?

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

Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¡Hola, Juli! 👋 ¿Cómo puedo ayudarte hoy?

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

Justo ahora el local está cerrado 😴 Abrimos hoy a las 19:00. ¡Hola, Lucas! 👋 Soy Ruperto, tu asistente de Mi Rotisería. ¿Qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una hamburguesa completa.

**Ruperto**

Anotado, Lucas: va 1 x Hamburguesa completa por $ 9.500. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

### Turn 3

**Customer**

Envío.

**Ruperto**

Dale. Pasame la dirección de envío, por favor.

- Next step: `ask_address`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

### Turn 4

**Customer**

¿El envío tiene costo?

**Ruperto**

Hacemos envíos, pero desde acá no tengo cargado un costo fijo para decírtelo automáticamente. Si querés, pasame la dirección o la zona y lo revisamos antes de cerrar el pedido.

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 9.500. Ítems: 1 x Hamburguesa completa. Avisos automáticos: True.

---

## 11. known_customer_repeat

- Persona: Cliente recurrente
- External user id: `qa-recurrente-1`
- Goal: Hace un pedido y luego vuelve a escribir para ver si conserva contexto.

### Turn 1

**Customer**

Hola, soy Elena.

**Ruperto**

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola, Elena! 👋 Te doy la bienvenida a Mi Rotisería. ¿Qué se te antoja hoy? Acá te dejo nuestro menú para que elijas:

*   **Comidas:**
    *   Combo hamburguesa completa: $ 13.800
    *   Docena de empanadas clásicas: $ 19.800
    *   Empanadas de carne: $ 1.800 (unidad)
    *   Empanadas de jamón y queso: $ 1.800 (unidad)
    *   Empanadas de pollo: $ 1.800 (unidad)
    *   Empanadas de verdura: $ 1.750 (unidad)
    *   Ensalada César: $ 8.700
    *   Hamburguesa BBQ: $ 11.500
    *   Hamburguesa clásica: $ 7.900
    *   Hamburguesa completa: $ 9.500
    *   Hamburguesa doble cheddar: $ 11.900
    *   Hamburguesa picante: $ 12.100
    *   Hamburguesa veggie: $ 10.300
    *   Lomito completo: $ 13.200
    *   Lomito especial: $ 14.200
    *   Milanesa a caballo: $ 12.800
    *   Milanesa completa: $ 11.900
    *   Milanesa napolitana: $ 12.500
    *   Papas cheddar y bacon: $ 6.900
    *   Papas fritas clásicas: $ 4.300
    *   Pizza calabresa: $ 14.800
    *   Pizza cuatro quesos: $ 15.400
    *   Pizza especial: $ 13.900
    *   Pizza fugazzeta: $ 12.800
    *   Pizza jamón y morrón: $ 14.400
    *   Pizza muzzarella: $ 11.000
    *   Pizza napolitana: $ 12.400
    *   Pizza rúcula y crudo: $ 16.800
    *   Sanguche de milanesa: $ 8.900
    *   Wrap de pollo: $ 9.200
    *   Wrap veggie: $ 8.800

*   **Bebidas:**
    *   Agua con gas 500ml: $ 1.900
    *   Agua saborizada pomelo 1.5L: $ 2.800
    *   Agua sin gas 500ml: $ 1.800
    *   Cerveza IPA lata: $ 3.400
    *   Cerveza roja lata: $ 3.100
    *   Cerveza rubia lata: $ 2.900
    *   Gaseosa cola 1.5L: $ 3.200
    *   Gaseosa cola cero 1.5L: $ 3.300
    *   Gaseosa lima-limón lata: $ 2.200
    *   Gaseosa naranja lata: $ 2.200

*   **Postres:**
    *   Brownie con nuez: $ 3.100
    *   Budín de pan: $ 2.900
    *   Cheesecake de frutos rojos: $ 5.400
    *   Flan casero: $ 3.500
    *   Helado 1/2 kg: $ 7.600
    *   Helado 1/4 kg: $ 4.200
    *   Tiramisú: $ 5.200

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una pizza napolitana.

**Ruperto**

Buenísimo, Elena: llevo 1 x Pizza napolitana por $ 12.400.  Si querés completar el pedido, podés sumar una bebida o un postre. ¿Te sirve algo más?

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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. Listo, Elena.

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

¡Sí, Elena! Recién pediste una Pizza Napolitana. 🍕

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

Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. La Pizza Fugazzeta sale $12.800. 🍕

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Y una cerveza IPA, ¿cuánto?

**Ruperto**

La Cerveza IPA en lata sale $3.400. 🍻 ¿Querés agregarla a tu pedido?

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

Justo ahora el local está cerrado 😴 Abrimos hoy a las 19:00. ¡Hola Carla! 👋 ¿Cómo puedo ayudarte hoy con tu pedido?

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

¡Hola, Diego! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. Igual podés hacer tu pedido, ¿qué te gustaría comer hoy?

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

### Turn 5

**Customer**

Link de pago.

**Ruperto**

¡Bárbaro! El pago sería con link.

Te recuerdo tu pedido:
*   2 Pizzas Muzzarella
*   1 Docena de Empanadas Clásicas
*   2 Gaseosas Cola 1.5L

Total: $48.200
Pago: Tarjeta/link
Envío a: 9 de Julio 1302, Anisacate.

¿Confirmamos todo, Diego? Así ya te mando el link.

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

¡Hola Belu! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. Igual, podés ir haciendo tu pedido si querés. ¿Qué te gustaría comer hoy?

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

Bien, Belu: tengo 1 x Tiramisú, 1 x Cheesecake de frutos rojos por $ 10.600. ¿Querés envío o retirás por el local?

- Next step: `choose_delivery`
- Order: Estado: draft. Total: $ 10.600. Ítems: 1 x Tiramisú, 1 x Cheesecake de frutos rojos. Avisos automáticos: True.

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

Justo ahora el local está cerrado 😴 Abrimos hoy a las 19:00. ¡Hola Pablo! 👋 ¿En qué puedo ayudarte hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una milanesa napolitana.

**Ruperto**

Dale, Pablo: tengo 1 x Milanesa napolitana por $ 12.500. Si querés, podés sumar unas papas, una bebida o un postre. ¿Te tienta algo más?

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

Perfecto. El total es $ 12.500. ¿Cómo querés pagar: efectivo, transferencia o link de pago?

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

Anotado, Pablo: va 1 x Hamburguesa BBQ por $ 11.500. Si te copa, le podés agregar unas papas, una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 11.500. Ítems: 1 x Hamburguesa BBQ. Avisos automáticos: True.

### Turn 8

**Customer**

Envío.

**Ruperto**

Buenísimo. ¿Va a Lavalle 12333? Si querés otro domicilio, decímelo.

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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola, Fer! 👋 ¿Qué te gustaría pedir hoy de Mi Rotisería?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Quiero una burger veggie.

**Ruperto**

Buenísimo, Fer: llevo 1 x Hamburguesa veggie por $ 10.300.  Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 10.300. Ítems: 1 x Hamburguesa veggie. Avisos automáticos: True.

### Turn 3

**Customer**

Sumame una cervecita.

**Ruperto**

¡Claro! Tenemos estas opciones de cervezas en lata para acompañar tu pedido: 🍺

*   Cerveza IPA lata: $3.400
*   Cerveza roja lata: $3.100
*   Cerveza rubia lata: $2.900

¿Cuál te gustaría sumar?

- Next step: `choose_items`
- Order: Estado: draft. Total: $ 10.300. Ítems: 1 x Hamburguesa veggie. Avisos automáticos: True.

---

## 18. menu_browse_then_order

- Persona: Cliente explorador
- External user id: `qa-carta-1`
- Goal: Pide la carta, recorre y recién después decide.

### Turn 1

**Customer**

Hola, ¿me pasás la carta?

**Ruperto**

Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¡Hola! 👋 Acá tenés nuestro menú:

**Comidas** 🍽️
- Empanadas de carne: $ 1.800
- Empanadas de pollo: $ 1.800
- Empanadas de jamón y queso: $ 1.800
- Empanadas de verdura: $ 1.750
- Docena de empanadas clásicas: $ 19.800
- Hamburguesa clásica: $ 7.900
- Hamburguesa completa: $ 9.500
- Hamburguesa doble cheddar: $ 11.900
- Hamburguesa BBQ: $ 11.500
- Hamburguesa veggie: $ 10.300
- Hamburguesa picante: $ 12.100
- Combo hamburguesa completa: $ 13.800
- Sanguche de milanesa: $ 8.900
- Milanesa completa: $ 11.900
- Milanesa napolitana: $ 12.500
- Milanesa a caballo: $ 12.800
- Lomito completo: $ 13.200
- Lomito especial: $ 14.200
- Pizza muzzarella: $ 11.000
- Pizza napolitana: $ 12.400
- Pizza fugazzeta: $ 12.800
- Pizza especial: $ 13.900
- Pizza calabresa: $ 14.800
- Pizza cuatro quesos: $ 15.400
- Pizza jamón y morrón: $ 14.400
- Pizza rúcula y crudo: $ 16.800
- Papas fritas clásicas: $ 4.300
- Papas cheddar y bacon: $ 6.900
- Ensalada César: $ 8.700
- Wrap de pollo: $ 9.200
- Wrap veggie: $ 8.800

**Bebidas** 🥤
- Agua sin gas 500ml: $ 1.800
- Agua con gas 500ml: $ 1.900
- Agua saborizada pomelo 1.5L: $ 2.800
- Gaseosa lima-limón lata: $ 2.200
- Gaseosa naranja lata: $ 2.200
- Gaseosa cola 1.5L: $ 3.200
- Gaseosa cola cero 1.5L: $ 3.300
- Cerveza rubia lata: $ 2.900
- Cerveza IPA lata: $ 3.400
- Cerveza roja lata: $ 3.100

**Postres** 🍰
- Flan casero: $ 3.500
- Tiramisú: $ 5.200
- Helado 1/4 kg: $ 4.200
- Helado 1/2 kg: $ 7.600
- Brownie con nuez: $ 3.100
- Cheesecake de frutos rojos: $ 5.400
- Budín de pan: $ 2.900

¿Qué te gustaría pedir? 😊

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Qué wraps hay?

**Ruperto**

Tenemos dos opciones de wraps:

- Wrap de pollo: $ 9.200 🌯
- Wrap veggie: $ 8.800 🥗

¿Te gustaría probar alguno? 😊

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

Justo ahora el local está cerrado 😴 Abrimos hoy a las 19:00. ¡Hola! 👋 Soy Ruperto, el asistente virtual de Mi Rotisería. Por el momento solo puedo tomar pedidos y responder preguntas sobre nuestro menú por acá. Si tenés alguna consulta que no pueda resolver, te derivo con un encargado.

- Next step: `handoff`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Puedo pagar con crypto?

**Ruperto**

¡Hola! Por ahora no aceptamos pagos con crypto. Podés pagar en efectivo, con tarjeta (Mercado Pago) o por transferencia. ¿Cuál preferís? 💳💰

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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola, Mica! 👋 Contame, ¿qué te gustaría pedir hoy?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿La ensalada César trae pollo sí o sí?

**Ruperto**

Sí, la Ensalada César viene con pollo grillado, lechuga, croutons, queso y aderezo César. 🥗 ¿Te gustaría pedirla o preferís ver otras opciones?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 3

**Customer**

¿Y la hamburguesa veggie tiene queso?

**Ruperto**

Sí, la Hamburguesa Veggie viene con medallón de garbanzos, queso, rúcula, tomate y papas rústicas. 🍔 ¿Te gustaría pedirla?

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

Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. Anotado, Tomi: va 1 x Pizza especial por $ 13.900. Si te copa, le podés agregar una bebida o un postre. ¿Querés sumar algo más?

- Next step: `choose_items`
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

¡Hola, Juani! 👋 Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. ¿Qué te gustaría pedir para cuando abramos?

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

Una hamburguesa BBQ.

**Ruperto**

Buenísimo, Juani: llevo 1 x Hamburguesa BBQ por $ 11.500.  Si querés completar el pedido, podés sumar unas papas, una bebida o un postre. ¿Te sirve algo más?

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

En este momento estamos fuera de horario 😴 Abrimos hoy a las 19:00. ¡Hola, Agus! 👋 Soy Ruperto, el asistente de Mi Rotisería. ¿En qué puedo ayudarte hoy?

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

**Ruperto**

Perfecto. El total es $ 16.800. ¿Cómo querés pagar: efectivo, transferencia o link de pago?

- Next step: `choose_payment`
- Order: Estado: draft. Total: $ 16.800. Ítems: 1 x Pizza rúcula y crudo. Avisos automáticos: True.

---

## 24. exact_price_comparison

- Persona: Cliente comparador
- External user id: `qa-comparador-1`
- Goal: Quiere comparar dos productos antes de decidir.

### Turn 1

**Customer**

¿Qué sale más, el lomito completo o la milanesa completa?

**Ruperto**

Ahora estamos cerrados 😴 Abrimos hoy a las 19:00. El lomito completo sale $13.200 y la milanesa completa $11.900. ¡El lomito es un poquito más caro! 😉

- Next step: `choose_items`
- Order: Sin pedido activo.

### Turn 2

**Customer**

¿Y cuál te parece más llenador?

**Ruperto**

¡Uhm, qué buena pregunta! Como soy un bot, no puedo opinar sobre cuál llena más, eso ya es muy personal. 😊 Pero te puedo asegurar que ambos son riquísimos. ¿Te gustaría saber algo más del menú o te ayudo a armar tu pedido?

- Next step: `choose_items`
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

¡Hola, Emi! 👋 Ahora estamos cerrados 😴, pero abrimos hoy a las 19:00. Podés ir viendo qué se te antoja para hoy a la noche. ¿Qué te gustaría pedir?

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
