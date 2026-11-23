import unittest

from autoscout.russian_refresh import _is_verified_rsl_match


class RussianRefreshMatcherTests(unittest.TestCase):
    def test_accepts_russian_translation_with_original_title_and_translation_clue(self):
        candidate = {"title": "48 Laws of Power", "primary_authors": ["Robert Greene"]}
        result = {
            "result_title": "48 законов власти = 48 laws of power / Роберт Грин; [пер. с англ.]",
            "result_author": "Грин, Роберт",
            "match_type": "ambiguous author/title match",
            "language": "Russian",
            "edition_url": "https://search.rsl.ru/ru/record/1",
        }
        self.assertTrue(_is_verified_rsl_match(candidate, result))

    def test_rejects_foreign_record_with_russian_page_abbreviations(self):
        candidate = {"title": "Elon Musk", "primary_authors": ["Ashlee Vance"]}
        result = {
            "result_title": "Elon Musk : how the billionaire CEO of SpaceX and Tesla is shaping our future",
            "result_author": "Vance, Ashlee",
            "match_type": "same-work Russian-language edition",
            "language": "Russian",
            "publication_data": "London, 2016. 402 с.",
            "edition_url": "https://search.rsl.ru/ru/record/2",
        }
        self.assertFalse(_is_verified_rsl_match(candidate, result))

    def test_rejects_map_record_that_contains_the_candidate_title(self):
        candidate = {"title": "The Land and Its People", "primary_authors": ["David Sedaris"]}
        result = {
            "result_title": "Te Ika a Māui : The Land and its people circa 1840 / New Zealand Geogr. Board",
            "result_author": "Северный остров (Новая Зеландия)",
            "match_type": "ambiguous author/title match",
            "language": "Russian",
            "edition_url": "https://search.rsl.ru/ru/record/3",
        }
        self.assertFalse(_is_verified_rsl_match(candidate, result))

    def test_accepts_verified_russian_author_note_for_translated_title(self):
        candidate = {
            "title": "Wie man Freunde gewinnt",
            "primary_authors": ["Dale Carnegie"],
            "russian_search_notes": {
                "search_translation": "Как завоёвывать друзей",
                "author_transliteration_note": "Дейл Карнеги",
            },
        }
        result = {
            "result_title": "Как завоёвывать друзей и оказывать влияние на людей = How to win friends & influence people",
            "result_author": "Карнеги, Дейл (1888-1955)",
            "match_type": "ambiguous author/title match",
            "language": "Russian",
            "edition_url": "https://search.rsl.ru/ru/record/4",
        }
        self.assertTrue(_is_verified_rsl_match(candidate, result))


if __name__ == "__main__":
    unittest.main()
