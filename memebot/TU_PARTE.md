# Lo que tienes que hacer TÚ (paso a paso, para dummies)

Todo lo demás ya está hecho: el bot, el cerebro, el panel, el instalador y el asistente que configura las claves.
Tú solo necesitas **crear 3 cuentas gratuitas, encender un servidor gratuito y pegar unas claves**.
Tiempo total: ~1 hora la primera vez.

> 💡 Ten abierto un **bloc de notas** y ve pegando ahí cada clave que consigas. Las necesitarás en el paso C.

---

## PASO A — Tres cuentas gratuitas (15 min)

### A1. Helius (conexión a Solana)
1. Entra en **https://dashboard.helius.dev** → *Sign up* (con Google o email).
2. Al entrar verás **API Keys** → copia la clave (letras y números, tipo `a1b2c3d4-...`).
3. 📋 Pégala en tu bloc de notas como **HELIUS**.

### A2. Jupiter (para comprar y vender)
1. Entra en **https://portal.jup.ag** → inicia sesión.
2. Ve a **API Keys** → *Create* / *Generate* → copia la clave.
3. 📋 Pégala en tu bloc de notas como **JUPITER**.

### A3. Telegram (avisos en el móvil; opcional pero muy recomendable)
1. En Telegram busca **@BotFather** → pulsa *Iniciar*.
2. Escribe `/newbot` → pon un nombre (por ejemplo `Mi MemeBot`) → luego un usuario que acabe en `bot` (por ejemplo `mimemebot123_bot`).
3. Te responde con un **token** (tipo `123456789:AAH...`). 📋 Pégalo como **TELEGRAM**.
4. El chat id no tienes que buscarlo: lo detecta el asistente.

---

## PASO B — Servidor gratuito 24/7 en Oracle Cloud (30 min)

1. Entra en **https://www.oracle.com/cloud/free/** → *Start for free*. Pide tarjeta solo para verificar: la capa
   *Always Free* no cobra. Elige como región la más cercana (por ejemplo *Spain Central (Madrid)* o *Germany Central
   (Frankfurt)*).
2. Ya dentro: menú ☰ → **Compute → Instances → Create instance**.
3. **Image**: pulsa *Edit* → **Ubuntu** → *Canonical Ubuntu 24.04*.
4. **Shape**: *Change shape* → **Ampere** → `VM.Standard.A1.Flex` → **2 OCPU y 12 GB** (gratis).
   - Si te sale *"Out of capacity"*, cambia el *Availability domain* (AD-2, AD-3) o vuelve a intentarlo más tarde.
5. **Add SSH keys**: deja *Generate a key pair for me* → pulsa **Save private key**. Se descarga un archivo `.key`.
   ⚠️ No lo pierdas: es la llave del servidor.
6. Pulsa **Create**. Espera 1–2 min hasta que ponga *Running* y copia la **Public IP address**. 📋 Pégala como **IP**.

### Conectarte al servidor
- **Windows**: abre **PowerShell** (busca "PowerShell" en Inicio) y escribe (cambia la ruta y la IP):
  ```
  ssh -i C:\Users\TU_USUARIO\Downloads\ssh-key.key ubuntu@TU_IP
  ```
- **Mac**: abre **Terminal** y escribe:
  ```
  chmod 400 ~/Downloads/ssh-key.key
  ssh -i ~/Downloads/ssh-key.key ubuntu@TU_IP
  ```
- Si pregunta *"Are you sure you want to continue connecting?"*, escribe `yes`. Ya estás dentro del servidor.

---

## PASO C — Instalar el bot (10 min; casi todo es automático)

Para descargar el código de tu repositorio (es privado), GitHub pide un **token de lectura**:
1. En **https://github.com/settings/personal-access-tokens/new**: nombre `memebot`; *Repository access* →
   *Only select repositories* → `test`; *Permissions* → *Contents* → **Read-only** → *Generate token*.
   📋 Cópialo.

En la ventana del servidor, pega estas líneas **una a una**:
```bash
sudo apt-get update && sudo apt-get install -y git
git clone -b claude/bold-johnson-evz8g1 https://github.com/danielcarbajo98/test.git
```
Cuando pida *Username*, escribe tu usuario de GitHub. Cuando pida *Password*, pega el **token** (no se ve al pegar:
es normal) y pulsa Enter.

```bash
cd test/memebot
sudo bash deploy/install.sh
```
Se instala solo. A los pocos minutos arranca el **asistente**, que te pregunta:

| Pregunta | Qué responder |
|---|---|
| Wallet: `[1]` nueva o `[2]` Phantom | Pulsa **Enter** (crea una nueva). **Copia la clave privada que muestra** y guárdala en un sitio seguro; luego pulsa Enter |
| API key de Helius | Pega **HELIUS** |
| API key de Jupiter | Pega **JUPITER** |
| ¿Avisos por Telegram? | `s` → pega **TELEGRAM** → abre tu bot en Telegram y escríbele "hola" → te llega "✅ MemeBot conectado" |
| Capital | **Enter** (50) |
| ¿Empezar ya con dinero real? | **Enter** (no: primero una semana de prueba) |

Al final te muestra la **dirección de tu wallet** (con un código QR) y la **URL del panel con su token**.
📋 Copia ambas.

Ya está funcionando 24/7 en modo prueba. **Puedes cerrar la ventana**: el servidor sigue solo.

---

## PASO D — Ver el panel desde tu ordenador

Abre una ventana nueva de PowerShell o Terminal y escribe (con tu ruta de la llave y tu IP):
```
ssh -L 8050:127.0.0.1:8050 -i RUTA_DE_TU_LLAVE.key ubuntu@TU_IP
```
Déjala abierta y entra en el navegador en la **URL del panel** que te dio el instalador
(`http://127.0.0.1:8050/?token=...`). Guárdala en favoritos.

---

## PASO E — Mientras prueba: comprar y enviar los 50 € (sin prisa)

1. Abre cuenta en **Bit2Me**, **Kraken** o **Coinbase** y verifica tu identidad (DNI + selfie).
2. Ingresa los **50 €** y compra:
   - **~45 € de USDC** (el capital);
   - **~5 € de SOL** (para comisiones).
3. **Retirar → USDC** → red **SOLANA** (⚠️ importantísimo: con otra red el dinero se pierde) → pega la **dirección
   de tu wallet** del paso C. Primero envía **5 USDC de prueba**; cuando lleguen, envía el resto.
4. Repite con el **SOL** (red Solana, a la misma dirección).
5. *(Opcional)* Para ver tu wallet en el móvil: en **Phantom** → *Añadir cuenta* → *Importar clave privada* → pega
   la clave privada del paso C.

---

## PASO F — Pasar a dinero real (tras ~1 semana)

Cuando en el panel la tarjeta **Cerebro** muestre una estrategia propia (ya no pone "Aprendiendo") y el dinero esté
en la wallet:
1. Conéctate al servidor (paso B) y escribe:
   ```bash
   cd test/memebot
   sudo nano config.yaml
   ```
2. Cambia `mode: paper` por **`mode: live`**. Guarda con **Ctrl+O**, **Enter** y sal con **Ctrl+X**.
3. ```bash
   sudo docker compose restart bot
   ```
4. En el panel verás **● DINERO REAL** y en Telegram te llegará "Bot iniciado en modo LIVE". A partir de aquí todo
   es automático.

---

## Comandos útiles (dentro de `test/memebot` en el servidor)

| Para… | Escribe |
|---|---|
| ver qué está haciendo | `sudo docker compose logs -f bot` (salir: Ctrl+C) |
| comprobar que todo funciona | `sudo docker compose ps` |
| cambiar claves o capital | `sudo bash deploy/install.sh` |
| actualizar a la última versión | `git pull && sudo docker compose up -d --build` |
| **emergencia: vender todo** | botón **Cerrar todo** del panel |
| parar el bot | `sudo docker compose down` |

## Las 3 reglas de oro
1. **Nunca** compartas la clave privada, el archivo `.env` ni el token del panel. Nadie legítimo te los pedirá.
2. Solo pon dinero que puedas perder entero.
3. Si algo te parece raro: botón **Cerrar todo** y me preguntas.
