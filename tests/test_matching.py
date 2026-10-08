import unittest

from paragraph_search.matching import match_details, match_ranges


class MatchingTests(unittest.TestCase):
    def test_original_unicode_offsets_survive_casefold_expansion_and_emoji(self):
        text = '😀 Straße ＡＩ 雨夜'
        ranges = match_ranges(text, ['strasse', 'ai', '雨夜'])
        self.assertEqual([text[start:end] for start, end in ranges], ['Straße', 'ＡＩ', '雨夜'])

    def test_repeated_and_overlapping_terms_merge_without_losing_text(self):
        self.assertEqual(match_ranges('雨夜雨夜', ['雨夜', '夜雨']), [[0, 4]])

    def test_combining_accents_highlight_the_original_cluster(self):
        text = 'cafe\u0301'
        ranges = match_ranges(text, ['café'])
        self.assertEqual(ranges, [[0, len(text)]])

    def test_explains_body_heading_and_mixed_hits(self):
        body = match_details('雨夜等待列车', '车站', '雨夜等待列车', '雨夜')
        self.assertEqual(body['match_kind'], 'body')
        heading = match_details('她等待列车', '雨夜素材', '她等待列车', '雨夜')
        self.assertEqual(heading['match_kind'], 'heading')
        self.assertEqual(heading['text_matches'], [])
        self.assertEqual(heading['heading_matches'], [[0, 2]])
        mixed = match_details('她等待列车', '雨夜素材', '她等待列车', '雨夜 列车')
        self.assertEqual(mixed['match_kind'], 'mixed')


if __name__ == '__main__':
    unittest.main()
