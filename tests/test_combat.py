from decimal import Decimal
import unittest


def settle(defense, spent, defense_multiplier, attack_multiplier):
    return (Decimal(defense) * Decimal(defense_multiplier)) - (Decimal(spent) * Decimal(attack_multiplier))


class CombatTests(unittest.TestCase):
    def test_unsuccessful_attack_keeps_positive_defense(self):
        self.assertEqual(settle("10", "2", "1", "2"), Decimal("6"))

    def test_exact_attack_neutralizes_territory(self):
        self.assertEqual(settle("10", "5", "1", "2"), Decimal("0"))

    def test_excess_attack_becomes_new_defense(self):
        remaining = settle("3", "2", "1.5", "3")
        self.assertEqual(remaining, Decimal("-1.5"))
        self.assertEqual(abs(remaining), Decimal("1.5"))
