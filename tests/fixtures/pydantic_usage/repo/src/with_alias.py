from pydantic import BaseModel as BM


class Product(BM):
    price: float
