"""Los tests NUNCA deben usar claves reales ni enviar mensajes (p. ej. cuando el actualizador
los ejecuta en el equipo del usuario, donde existen esas variables)."""
import pytest

SECRET_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "WALLET_PRIVATE_KEY",
               "JUPITER_API_KEY", "GITHUB_TOKEN", "DASHBOARD_TOKEN", "SOLANA_RPC_URL")


@pytest.fixture(autouse=True)
def _no_secrets_no_messages(monkeypatch):
    for v in SECRET_VARS:
        monkeypatch.delenv(v, raising=False)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # no leer un .env real
    import bot.config
    monkeypatch.setattr(bot.config, "load_dotenv", lambda *a, **k: False)
    # el ciclo del bot no debe leer noticias ni listas de Jupiter reales (resultados que cambian cada día)
    import bot.context
    import bot.main
    monkeypatch.setattr(bot.context, "build", lambda *a, **k: {})
    monkeypatch.setattr(bot.main, "jupiter_discover", lambda *a, **k: ([], {}))
    yield
