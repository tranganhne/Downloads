from service.order_service import OrderService


class OrderController:
    def __init__(self):
        self.svc = OrderService()

    def create(self, data):
        return self.svc.place_order(data)
