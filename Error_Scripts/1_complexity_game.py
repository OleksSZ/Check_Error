def handle_turn(weapon, enemy, has_potion, is_night, difficulty):
    if weapon == "sword":
        if enemy == "dragon":
            if is_night:
                damage = 10
            else:
                damage = 15
        elif enemy == "goblin":
            damage = 20
        elif enemy == "troll":
            damage = 5
        else:
            damage = 1
    else:
        damage = 0

    if has_potion and difficulty == "hard":
        damage += 5

    return damage