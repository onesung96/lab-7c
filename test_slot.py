# python test_slot.py — 내 슬롯 고르기(my_slot) 점검. 설정 파일은 건드리지 않는다.
import ordr_helper as H

H.save_cfg = lambda: None


def case(names, me, cand):
    H.game['names'] = names
    H.cfg.pop('my_name', None)
    if me:
        H.cfg['my_name'] = me
    return H.my_slot(cand), H.cfg.get('my_name')


assert case([], None, [3]) == (3, None)                                   # 이름을 못 읽음: 예전처럼 후보가 하나면 그 슬롯
assert case([], None, [1, 3]) == (None, None)
assert case([(0, 'A#1')], None, [0]) == (0, 'A#1')                        # 혼자 하는 판: 이름을 배운다
assert case([(0, 'A#1')], 'Old#9', [0]) == (0, 'A#1')                     # 닉네임을 바꿨으면 다시 배운다
assert case([(0, 'B#2'), (1, 'A#1')], 'A#1', [0, 1]) == (1, 'A#1')        # 여럿: 내 이름이 붙은 슬롯
assert case([(0, 'B#2'), (1, 'A#1')], 'A#1', [0]) == (None, 'A#1')        # 내 유닛이 아직 없음: 남의 슬롯을 고르지 않는다
assert case([(0, 'B#2'), (1, 'A#1')], None, [0]) == (None, None)          # 내 이름을 모름: 추측하지 않고, 남의 이름도 안 배운다
assert case([(0, 'A#1'), (1, 'A#1'), (2, 'B#2')], 'A#1', [0, 1, 2]) == (None, 'A#1')   # 같은 이름이 둘: 모른다
print('ok')
