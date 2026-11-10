from repo.order_repo import OrderRepo
from api.order_controller import OrderController   # vi phạm: service -> api


class OrderService:
    def __init__(self):
        self.repo = OrderRepo()

    def place_order(self, data):
        return self.repo.save(data)
