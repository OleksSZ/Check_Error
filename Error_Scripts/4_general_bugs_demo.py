"""
навмисно максимально зламаний файл.

Тут навмисно понатикано різні порушення ООП і логічні баги,
щоб перевірити, як oopCheck.py їх знаходить і виводить у checks.log.
Не використовуйте цей код як приклад гарного стилю )
"""

import json
import re  # імпорт ніде у файлі не використовується


class shopping_cart:  # 1. ім'я класу не в PascalCase

    items = []  # 2. змінюваний атрибут рівня класу — спільний для ВСІХ кошиків одразу!
    discounts = {}  # теж спільний словник на всі екземпляри

    def __init__(self, owner):
        self.owner = owner
        # total тут не створюється -> ATTR-MISS спрацює нижче

    def add_item(self, name, price=[]):  # 3. mutable default
        self.items.append((name, price))
        total = self.get_total()  # 4. змінній total присвоюється значення, але вона не використовується (UNUSED-VAR)

    def get_total(self):
        return self.total  # 5. читаємо self.total, який ніде не присвоювався (ATTR-MISS)

    def apply_discount(self, code):
        if code == None:  # 6. порівняння з None через ==
            return
        if code is "SALE10":  # 7. порівняння рядка через is (IS-LITERAL)
            self.discounts[code] = 0.1

    def print_owner(self):  # 8. не використовує self -> кандидат у @staticmethod
        print("Cart owner")

    def risky_parse(self, raw):
        try:
            return json.loads(raw)
        except:  # 9. голий except
            pass  # 10. + except з одним pass -> тихо ковтає помилку

    def compute_price(self, base_price):
        if base_price == 19.99:  # 11. порівняння float через ==
            return base_price * 0.9
        return base_price


class PremiumCart(shopping_cart):  # 12. нащадок...
    def __init__(self, owner, vip_level):
        # ...але НЕ викликає super().__init__(owner) -> owner не буде встановлено
        self.vip_level = vip_level

    def __eq__(self, other):  # 13. визначено __eq__ без __hash__
        return self.vip_level == other.vip_level


def endless_worker():
    while True:  # 14. нескінченний цикл без break
        process_next_item()


def process_next_item():
    pass


def build_report(a, b, c, d, e, f, g, h):  # 15. забагато параметрів (8)
    return a + b + c + d + e + f + g + h


def find_user(users, target_id):
    for user in users:
        if user["id"] == target_id:
            result = user  # 16. змінній result присвоюється значення, але return поза циклом її не використовує
    return None  # забули повернути result -> логічний баг