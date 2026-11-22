from .models import Blueprint, Expansion
from .recognition import Recognition, match
from .tests import LedgerTestCase, cm_id, load_catalog, game

FABLE = "Fable of the Mirror-Breaker"


class MatchBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        load_catalog()
        self.neo = Expansion.objects.get(code="neo")
        self.magic = game("mtg")


class MatchTest(MatchBase):
    def test_number_in_locked_expansion(self):
        m = match(Recognition(name=FABLE, collector_number="0356", language="IT"), self.neo, self.magic)
        self.assertEqual((m.reason, m.blueprint.cm_product_id), ("ok", cm_id(1101)))

    def test_name_only_with_versions_is_ambiguous(self):
        m = match(Recognition(name=FABLE, language="EN"), self.neo, self.magic)
        self.assertEqual(m.reason, "ambiguous")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.cm_product_id for b in m.candidates], [cm_id(1100), cm_id(1101)])

    def test_all_expansions_uses_the_printed_set(self):
        rec = Recognition(name="Sheoldred, the Apocalypse", set_code="dmu", collector_number="107", language="EN")
        m = match(rec, None, self.magic)
        self.assertEqual((m.reason, m.blueprint.cm_product_id), ("ok", cm_id(1301)))
        self.assertEqual(self.ct.calls, [])  # the catalog is local: CardTrader is never asked

    def test_unknown_set_code_not_found(self):
        m = match(Recognition(name="Nothing", set_code="zzz", language="EN"), None, self.magic)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))

    def test_name_number_mismatch_is_ambiguous(self):
        m = match(Recognition(name=FABLE, collector_number="999", language="EN"), self.neo, self.magic)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual({b.cm_product_id for b in m.candidates}, {cm_id(1100), cm_id(1101)})

    def test_split_card_front_face_matches(self):
        Blueprint.objects.create(cm_product_id=cm_id(1199), expansion=self.neo, name="Fire // Ice", collector_number="400")
        m = match(Recognition(name="Fire", language="EN"), self.neo, self.magic)
        self.assertEqual((m.reason, m.blueprint.cm_product_id), ("ok", cm_id(1199)))

    def test_unreadable_language_needs_confirmation(self):
        m = match(Recognition(name=FABLE, collector_number="356", language=""), self.neo, self.magic)
        self.assertEqual(m.reason, "language")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.cm_product_id for b in m.candidates], [cm_id(1101)])


class MatchReviewFixesTest(MatchBase):
    def test_locked_expansion_follows_other_printed_set(self):  # I2
        rec = Recognition(name="Counterspell", set_code="dmr", collector_number="45", language="EN")
        m = match(rec, self.neo, self.magic)
        self.assertEqual((m.reason, m.blueprint.cm_product_id), ("ok", cm_id(1201)))

    def test_name_only_across_loaded_sets_is_uncertain(self):  # I3
        m = match(Recognition(name=FABLE + "", collector_number="356", language="EN"), None, self.magic)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual([b.cm_product_id for b in m.candidates], [cm_id(1101)])

    def test_unknown_codes_never_call_cardtrader(self):  # I5
        match(Recognition(name="X", set_code="zz1", language="EN"), None, self.magic)
        match(Recognition(name="Y", set_code="zz2", language="EN"), None, self.magic)
        self.assertEqual(self.ct.calls, [])


class PromoNumberMismatchTest(MatchBase):
    """Issue #14: a promo read as PRM #1 must not be offered as another card numbered 1."""

    def setUp(self):
        super().setUp()
        self.prm = Expansion.objects.create(game=game(), code="prm", name="30th Anniversary Promos")
        for cm, name in [(268473, "Destroy Evil"), (219682, "Arcane Signet"), (262181, "Lotus Petal"),
                         (219684, "Richard Garfield, Ph.D.")]:
            Blueprint.objects.create(cm_product_id=cm, expansion=self.prm, name=name, collector_number="1")
        self.mrd = Expansion.objects.create(game=game(), code="mrd", name="Mirrodin")
        Blueprint.objects.create(cm_product_id=5010, expansion=self.mrd, name="Gilded Lotus", collector_number="202")

    def rec(self, name="Gilded Lotus"):
        return Recognition(name=name, set_code="PRM", collector_number="1", language="EN", foil=True)

    def test_name_found_outside_the_scope_comes_first(self):
        m = match(self.rec(), None, self.magic)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual(m.candidates[0].cm_product_id, 5010)

    def test_every_number_hit_is_considered(self):
        m = match(self.rec("Lotus Petal"), None, self.magic)
        self.assertEqual((m.reason, m.blueprint.cm_product_id), ("ok", 262181))

    def test_number_hits_follow_name_matches(self):
        m = match(self.rec(), None, self.magic)
        self.assertEqual([b.cm_product_id for b in m.candidates], [5010, 219682, 219684, 262181, 268473])

    def test_name_in_scope_hides_other_expansions(self):
        Blueprint.objects.create(cm_product_id=268999, expansion=self.prm, name="Gilded Lotus",
                                 collector_number="P1")
        m = match(self.rec(), None, self.magic)
        self.assertEqual([b.cm_product_id for b in m.candidates][:1], [268999])
        self.assertNotIn(5010, [b.cm_product_id for b in m.candidates])

    def test_name_only_falls_back_outside_the_scope(self):
        rec = Recognition(name="Gilded Lotus", set_code="PRM", language="EN")
        m = match(rec, None, self.magic)
        self.assertEqual((m.reason, [b.cm_product_id for b in m.candidates]), ("ambiguous", [5010]))

    def test_unknown_name_offers_number_hits(self):
        m = match(self.rec("Nothing Like It"), None, self.magic)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual(len(m.candidates), 4)


class GameMatchTest(MatchBase):
    """The game is a parameter: a session of another game never sees Magic printings, and vice versa."""

    def setUp(self):
        super().setUp()
        self.pokemon = game("pokemon")
        self.sv1 = Expansion.objects.create(game=self.pokemon, code="sv1", name="Scarlet & Violet")
        self.pikachu = Blueprint.objects.create(cm_product_id=700001, expansion=self.sv1, name="Pikachu",
                                                collector_number="025")

    def test_matches_in_the_game_given(self):
        m = match(Recognition(name="Pikachu", set_code="sv1", collector_number="025", language="EN"), None, self.pokemon)
        self.assertEqual((m.reason, m.blueprint), ("ok", self.pikachu))
        m = match(Recognition(name=FABLE, set_code="neo", collector_number="356", language="EN"), None, self.pokemon)
        self.assertEqual((m.reason, m.candidates), ("not_found", []))

    def test_zero_padded_catalog_number_matches_by_number(self):
        """The catalog keeps the generator's number ("025"); the number read is compared normalized."""
        for read in ("25", "025", "0025"):
            with self.subTest(read=read):
                m = match(Recognition(set_code="sv1", collector_number=read, language="EN"), None, self.pokemon)
                self.assertEqual((m.reason, m.blueprint), ("ok", self.pikachu))


class OtherGamesNamesTest(LedgerTestCase):
    """Catalog names of other games carry attacks / codes; Yu-Gi-Oh! codes can be slugs."""

    def setUp(self):
        super().setUp()
        self.pokemon, self.onepiece, self.yugioh = game("pokemon"), game("onepiece"), game("yugioh")
        self.pal = Expansion.objects.create(game=self.pokemon, code="pal", name="Paldea Evolved")
        self.arboliva = Blueprint.objects.create(cm_product_id=900001, expansion=self.pal, collector_number="21",
                                                 name="Arboliva [Enriching Oil | Solar Beam]")

    def test_pokemon_name_with_attacks_is_certain(self):
        rec = Recognition(name="Arboliva", set_code="pal", collector_number="021", language="EN")
        m = match(rec, None, self.pokemon)
        self.assertEqual((m.reason, m.blueprint), ("ok", self.arboliva))

    def test_onepiece_name_with_code_is_certain(self):
        op07 = Expansion.objects.create(game=self.onepiece, code="op07", name="500 Years in the Future")
        lucci = Blueprint.objects.create(cm_product_id=900002, expansion=op07, collector_number="OP07-079",
                                         name="Rob Lucci (OP07-079)")
        rec = Recognition(name="Rob Lucci", set_code="op07", collector_number="OP07-079", language="EN")
        m = match(rec, None, self.onepiece)
        self.assertEqual((m.reason, m.blueprint), ("ok", lucci))

    def test_name_only_match_ignores_suffix(self):
        m = match(Recognition(name="Arboliva", set_code="pal", language="EN"), None, self.pokemon)
        self.assertEqual((m.reason, m.blueprint), ("ok", self.arboliva))

    def test_different_card_with_same_prefix_is_not_matched(self):
        Blueprint.objects.create(cm_product_id=900003, expansion=self.pal, collector_number="22",
                                 name="Arboliva ex [Big Hit]")
        m = match(Recognition(name="Arboliva", set_code="pal", language="EN"), None, self.pokemon)
        self.assertEqual((m.reason, m.blueprint), ("ok", self.arboliva))
        Blueprint.objects.filter(pk=self.arboliva.pk).delete()
        m = match(Recognition(name="Arboliva", set_code="pal", language="EN"), None, self.pokemon)
        self.assertEqual((m.reason, m.candidates), ("not_found", []))

    def test_magic_suffix_is_not_stripped(self):
        load_catalog()
        neo = Expansion.objects.get(code="neo")
        Blueprint.objects.create(cm_product_id=900004, expansion=neo, collector_number="T1", name="Spirit Token (White 1/1)")
        m = match(Recognition(name="Spirit Token", set_code="neo", language="EN"), neo, game("mtg"))
        self.assertEqual((m.reason, m.blueprint), ("not_found", None))

    def test_slug_expansion_code(self):
        from . import catalog
        agov = Expansion.objects.create(game=self.yugioh, code="agov-age-of-overlord", name="Age of Overlord")
        self.assertEqual(catalog.expansion_by_code("AGOV", self.yugioh), agov)
        Expansion.objects.create(game=self.yugioh, code="agov-other", name="Other")
        self.assertIsNone(catalog.expansion_by_code("agov", self.yugioh))
        exact = Expansion.objects.create(game=self.yugioh, code="agov", name="Exact")
        self.assertEqual(catalog.expansion_by_code("agov", self.yugioh), exact)
