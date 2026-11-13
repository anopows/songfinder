from songfinder.presentation import _summarise


def test_confirmed_recording_identity_cannot_be_overwritten_by_a_hint():
    winner = {'decision': 'exact', 'title': 'Night and Day (Charlie Barnet Version)',
              'artist': 'Billy May', 'album': 'A Tribute', 'source': 'bandcamp',
              'url': 'https://example.bandcamp.com/track/night-and-day',
              'audio': 'full', 'similarity': .9, 'rate': 1, 'reference_offset_s': 3}
    report = {'results': [winner], 'shazam_hints': [
        {'title': 'Night and Day', 'artist': 'Charlie Barnet'}]}
    result = _summarise(report)
    assert result['artist'] == 'Billy May'
    assert result['title'] == winner['title']
    assert result['album'] == 'A Tribute'
    assert result['links'][0]['url'] == winner['url']


def test_missing_winner_artist_is_not_filled_from_unverified_hint():
    report = {'results': [{'decision': 'exact', 'title': 'A Song', 'artist': '',
                          'url': 'https://example.bandcamp.com/track/a-song',
                          'audio': 'full', 'similarity': .8}],
              'shazam_hints': [{'title': 'A Song', 'artist': 'Unverified Artist'}]}
    assert _summarise(report)['artist'] == ''

def test_wrong_performer_hint_does_not_supply_listening_links():
    from songfinder.links import _hint_for
    assert _hint_for({'shazam_hints':[{'title':'A Song','artist':'Wrong Band'}]},
                     {'title':'A Song','artist':'Right Band'})==[]

def test_incomplete_searches_have_distinct_states():
    assert _summarise({'results':[{'decision':'related'}]})['verdict']=='related'
    assert _summarise({'provider_issues':['unavailable']})['verdict']=='unavailable'
    assert _summarise({'shazam_hints':[{'title':'A Song'}]})['verdict']=='unconfirmed'
    assert _summarise({})['verdict']=='none'
