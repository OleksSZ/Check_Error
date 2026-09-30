from base import Animal


class Dog(Animal):
    def __init__(self, name, breed):
        # забыли super().__init__(name) -> self.name/self.energy никогда не установятся!
        self.breed = breed

    def bark(self):
        print(f"{self.name} says woof")  # читает self.name - его нет ни тут, ни в Animal.__init__ не вызван

    def rest(self):
        self.energy -= 5  # тоже читает self.energy


import unittest


class DogTest(unittest.TestCase):  # родитель ВНЕ проекта -> не должно быть ложных ATTR-MISS/NO-SUPER-INIT
    def test_something(self):
        d = Dog("Rex", "Labrador")
        self.assertEqual(d.breed, "Labrador")