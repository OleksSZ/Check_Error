"""
специально  максимально сломанный файл.

Здесь намеренно понатыканы разные ООП нарушения и логические баги,
чтобы проверить, как oop_checker.py их находит и выводит в checks.log.
Не используйте этот код как пример хорошего стиля )
"""

import json
import re  # импорт не используется нигде в файле


class shopping_cart:  # 1. имя класса не в PascalCase

    items = []  # 2. мутабельный class-level атрибут — общий для ВСЕХ корзин сразу!
    discounts = {}  # тоже общий словарь на все экземпляры

    def __init__(self, owner):
        self.owner = owner
        # total не создаётся здесь -> ATTR-MISS сработает ниже

    def add_item(self, name, price=[]):  # 3. mutable default
        self.items.append((name, price))
        total = self.get_total()  # 4. переменная total присваивается, но не используется (UNUSED-VAR)

    def get_total(self):
        return self.total  # 5. читаем self.total, который нигде не присваивался (ATTR-MISS)

    def apply_discount(self, code):
        if code == None:  # 6. сравнение с None через ==
            return
        if code is "SALE10":  # 7. сравнение строки через is (ID-MISUSE)
            self.discounts[code] = 0.1

    def print_owner(self):  # 8. не использует self -> кандидат в @staticmethod
        print("Cart owner")

    def risky_parse(self, raw):
        try:
            return json.loads(raw)
        except:  # 9. голый except
            pass  # 10. + except с одним pass -> тихо проглатывает ошибку

    def compute_price(self, base_price):
        if base_price == 19.99:  # 11. сравнение float через ==
            return base_price * 0.9
        return base_price


class PremiumCart(shopping_cart):  # 12. наследник...
    def __init__(self, owner, vip_level):
        # ...но НЕ вызывает super().__init__(owner) -> owner не будет установлен
        self.vip_level = vip_level

    def __eq__(self, other):  # 13. определён __eq__ без __hash__
        return self.vip_level == other.vip_level


def endless_worker():
    while True:  # 14. бесконечный цикл без break
        process_next_item()


def process_next_item():
    pass


def build_report(a, b, c, d, e, f, g, h):  # 15. слишком много параметров (8)
    return a + b + c + d + e + f + g + h


def find_user(users, target_id):
    for user in users:
        if user["id"] == target_id:
            result = user  # 16. переменная result присваивается, но return вне цикла её не использует
    return None  # забыли вернуть result -> логический баг