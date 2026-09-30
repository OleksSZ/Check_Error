class Animal:
    def __init__(self, name):
        self.name = name
        self.energy = 100

    def eat(self):
        self.energy += 10