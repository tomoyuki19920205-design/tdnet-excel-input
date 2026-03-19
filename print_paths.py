from src.config import load_config
c = load_config()
print("state_db_path =", c.state_db_path)
print("decision_db_path =", c.decision_db_path)
