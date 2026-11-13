from leeroy_mind.selfies import ration
from leeroy_mind.selfies import scene_choices as choices


def test_dial_three_is_two_a_day_and_old_private_table_is_unchanged():
    assert ration.CHANNEL_PER_DAY == (0,.5,1,2,3,4,5,6,8,10,12)
    assert ration.PER_WEEK == (0,1,2,3,5,7,10,14,20,28,40)
    assert ration.channel_per_day(3) == 2
    assert ration.him_stock(3) == ration.channel_stock(3) == 6
    assert ration.channel_stock(10) == 20
    assert not ration.channel_room(0,today=0)
    assert ration.him_stock(0) == ration.channel_stock(0) == 0
    assert ration.channel_room(3,today=1) and not ration.channel_room(3,today=2)
    assert ration.channel_room(1,today=0,draw=.49)
    assert not ration.channel_room(1,today=0,draw=.5)


def test_every_scene_value_maps_to_one_channel():
    for enums, table in ((choices.ACTIVITIES, choices.ACTIVITY_CHANNEL),
                         (choices.PLACES, choices.PLACE_CHANNEL),
                         (choices.PROPS, choices.PROP_CHANNEL)):
        assert set(enums) == set(table)
        assert set(table.values()) <= {'food','pets','irl','pics'}
    assert choices.channel_for({'activity':choices.ACTIVITIES['cooking'][0],
                                'setting':choices.PLACES['kitchen']}) == 'food'
    assert choices.channel_for({'activity':choices.ACTIVITIES['dog_walk'][0] + ', ' + choices.PROPS['leash'],
                                'setting':choices.PLACES['park']}) == 'pets'
    assert choices.channel_for({'activity':choices.ACTIVITIES['spreadsheet'][0],
                                'setting':choices.PLACES['desk']}) == 'irl'
    assert choices.channel_for({'activity':choices.ACTIVITIES['raiding'][0],
                                'setting':choices.PLACES['desk']}) == 'pics'
