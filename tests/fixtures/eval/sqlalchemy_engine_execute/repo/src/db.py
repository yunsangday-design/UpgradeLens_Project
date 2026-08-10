from sqlalchemy import engine
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()
result = engine.execute("SELECT 1")
