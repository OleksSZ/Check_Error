from base import Animal


class Dog(Animal):
    def __init__(self, name, breed):
        # забули super().__init__(name) -> self.name/self.energy ніколи не встановляться!
        self.breed = breed

    def bark(self):
        print(f"{self.name} says woof")  # читає self.name - його немає ні тут, ні в Animal (__init__ не викликано)

    def rest(self):
        self.energy -= 5  # теж читає self.energy


import unittest


class DogTest(unittest.TestCase):  # батьківський клас ПОЗА проєктом -> не має бути хибних ATTR-MISS/NO-SUPER-INIT
    def test_something(self):
        d = Dog("Rex", "Labrador")
        self.assertEqual(d.breed, "Labrador")