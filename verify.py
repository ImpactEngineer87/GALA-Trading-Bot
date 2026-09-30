import importlib, sys
sys.path.insert(0, ".")
for m in ["pandas","numpy","flask","requests","yaml","dotenv","xgboost","sklearn","joblib","pyarrow"]:
    importlib.import_module(m)
print("DEPS OK")
import server                      # importa la app Flask (no la arranca)
import engine.bot_engine           # importa el motor y todos sus modulos
import api.hybrid_order_executor, api.schwab_client, notifications.telegram_bot
print("MODULOS DEL BOT OK")
import yaml
c = yaml.safe_load(open("config.yaml", encoding="utf-8"))
print("CONFIG: tickers", c["market"]["tickers"], "| decision", c["decision"]["timeframe"],
      "| paper", c["risk"]["paper_trading"], "| telegram_hitl", c["telegram"]["hitl"]["activo"])
