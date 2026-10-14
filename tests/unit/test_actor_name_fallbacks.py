"""上游中文字段、汉字别名回退及完整姓名匹配回归。"""

from importlib.resources import files

import pytest
from lxml import etree

from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.metadata.actors import ActorMapping, ActorMappingError
from javsp.metadata.summarizer import MetadataMerger
from javsp.publish.naming import generate_names
from javsp.publish.nfo import write_nfo
from javsp.workflow.models import CrawlResult

XML = '''<actor-mapping>
  <a jp="二階堂麗" zh_cn="二階堂麗" keyword="二阶堂丽,红音丽,あかね麗" />
  <a jp="藤浦めぐ" zh_cn="藤浦惠" keyword="めぐり" />
  <a jp="森沢かな" zh_cn="森泽佳奈" keyword="飯岡かなこ" />
  <a jp="彩月七緒" zh_cn="彩月七緒" />
  <a jp="蒼山愛奈" zh_cn="蒼山愛奈" />
  <a jp="雛形みくる" zh_cn="雛形みくる" keyword="雏形美久琉" />
  <a jp="神宮寺ナオ" zh_cn="神宮寺ナオ" zh_tw="神宮寺ナオ" keyword="松岡芽郁,池田充希,神宮寺奈緒" />
</actor-mapping>'''.encode()


@pytest.mark.parametrize(('name', 'expected', 'alias_off'), [
    ('あかね麗', '二階堂麗（二阶堂丽）', 'あかね麗（二阶堂丽）'),
    ('めぐり', '藤浦めぐ（藤浦惠）', 'めぐり（藤浦惠）'),
    ('森沢かな', '森沢かな（森泽佳奈）', '森沢かな（森泽佳奈）'),
    ('彩月七緒', '彩月七緒（彩月七绪）', '彩月七緒（彩月七绪）'),
    ('蒼山愛奈', '蒼山愛奈（苍山爱奈）', '蒼山愛奈（苍山爱奈）'),
    ('雛形みくる', '雛形みくる（雏形美久琉）', '雛形みくる（雏形美久琉）'),
    ('神宮寺ナオ', '神宮寺ナオ（神宫寺奈绪）', '神宮寺ナオ（神宫寺奈绪）'),
    ('神宮寺奈緒', '神宮寺ナオ（神宫寺奈绪）', '神宮寺奈緒（神宫寺奈绪）'),
    ('サンディー・雛・パークス', 'サンディー・雛・パークス', 'サンディー・雛・パークス'),
])
def test_reported_names(name, expected, alias_off):
    mapping = ActorMapping.from_xml(XML)
    for normalize, result in ((True, expected), (False, alias_off)):
        assert mapping.resolve(name, normalize_aliases=normalize) == result
        assert mapping.resolve(result, normalize_aliases=normalize) == result


@pytest.fixture
def reported_mapping():
    xml = XML.replace(b'</actor-mapping>', '''
      <a jp="桜ゆの" zh_cn="桜ゆの" zh_tw="桜ゆの" keyword="櫻柚乃,樱柚乃" />
      <a jp="三澄寧々" zh_cn="三澄寧々" zh_tw="三澄寧々" keyword="三澄宁宁" />
      <a jp="七嶋舞" zh_cn="七嶋舞" zh_tw="七嶋舞" keyword="七岛舞" />
    </actor-mapping>'''.encode())
    supplement = files('javsp.metadata').joinpath('data/actor-supplement.xml').read_bytes()
    return ActorMapping.from_xml(xml, supplement=supplement)


@pytest.mark.parametrize(('name', 'expected'), [
    ('あかね麗（二階堂麗）', 'あかね麗（二阶堂丽）'),
    ('めぐり（藤浦めぐ）', 'めぐり（藤浦惠）'),
    ('森沢かな（飯岡かなこ）', '森沢かな（森泽佳奈）'),
    ('めぐり(藤浦めぐ)', 'めぐり（藤浦惠）'),
])
def test_same_actor_composite_name_uses_first_name(name, expected):
    mapping = ActorMapping.from_xml(XML)
    assert mapping.resolve(name) == expected
    assert mapping.resolve(name, normalize_aliases=False) == expected
    assert mapping.resolve(expected) == expected


def test_simplification_does_not_override_new_upstream_translation():
    mapping = ActorMapping.from_xml(XML.replace('zh_cn="彩月七緒"'.encode(), 'zh_cn="新译名"'.encode()))
    assert mapping.resolve('彩月七緒') == '彩月七緒（新译名）'


@pytest.mark.parametrize(('rows', 'expected'), [
    ('<a jp="神宮寺ナオ" />', '神宮寺ナオ'),
    ('<a jp="神宮寺ナオ" zh_cn="上游译名" keyword="神宮寺奈緒" />', '神宮寺ナオ（上游译名）'),
    ('<a jp="神宮寺ナオ" zh_cn="神宮寺ナオ" zh_tw="神宮寺奈緒" keyword="神宮寺奈緒" />', '神宮寺ナオ（神宮寺奈緒）'),
    ('<a jp="神宮寺ナオ" zh_cn="译名甲" keyword="神宮寺奈緒" /><a jp="神宮寺ナオ" zh_cn="译名乙" />', '神宮寺ナオ'),
    ('<a jp="其他原名" />', '神宮寺ナオ'),
])
def test_jinguji_translation_requires_upstream_evidence(rows, expected):
    mapping = ActorMapping.from_xml(f'<actor-mapping>{rows}</actor-mapping>'.encode())
    assert mapping.resolve('神宮寺ナオ') == expected


@pytest.mark.parametrize(('jp', 'keywords', 'expected'), [
    ('架空あおい', '旧名花子,架空葵', '架空あおい（架空葵）'),
    ('白花にあ', '白花仁愛,白花仁爱', '白花にあ（白花仁爱）'),
    ('愛川すず', '爱川铃,愛川鈴', '愛川すず（爱川铃）'),
    ('白石まゆ', '白石真由,白石麻友', '白石まゆ'),
    ('白石まゆ', '前白石真由,別姓真由,白石mayu,白石ま由', '白石まゆ'),
    ('白石まゆ', '白石', '白石まゆ'),
    ('あおい', '葵', 'あおい'),
    ('葵あおい', '葵青衣', '葵あおい（葵青衣）'),
    ('架空・あおい', '架空葵', '架空・あおい'),
    ('架空Aあおい', '架空葵', '架空Aあおい'),
    ('架空あお美', '架空青美,架空青子', '架空あお美（架空青美）'),
    ('架あお空', '架青空,架青子', '架あお空（架青空）'),
])
def test_keyword_fallback_matches_kanji_segments_without_name_overrides(jp, keywords, expected):
    mapping = ActorMapping.from_xml(
        f'<actor-mapping><a jp="{jp}" zh_cn="{jp}" keyword="{keywords}" /></actor-mapping>'.encode()
    )
    assert mapping.resolve(jp) == expected
    assert mapping.resolve(jp, normalize_aliases=False) == expected
    assert mapping.resolve(expected) == expected


@pytest.mark.parametrize('other', [
    '<a jp="別人" keyword="白花仁爱" />',
    '<a jp="別人" zh_cn="白花仁爱" />',
    '<a jp="白花仁爱" />',
])
def test_simplified_alias_ownership_is_checked_across_all_indexes(other):
    mapping = ActorMapping.from_xml(f'''<actor-mapping>
      <a jp="白花にあ" keyword="白花仁愛" />{other}
    </actor-mapping>'''.encode())
    assert mapping.resolve('白花にあ') == '白花にあ'


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize(('second', 'expected'), [
    ('白花仁爱', '白花にあ（白花仁爱）'),
    ('白花新名', '白花にあ'),
])
def test_duplicate_rows_merge_equivalent_candidates_but_preserve_conflicts(reverse, second, expected):
    rows = ['<a jp="白花にあ" keyword="白花仁愛" />', f'<a jp="白花にあ" keyword="{second}" />']
    if reverse:
        rows.reverse()
    mapping = ActorMapping.from_xml(('<actor-mapping>' + ''.join(rows) + '</actor-mapping>').encode())
    assert mapping.resolve('白花にあ') == expected


def test_kanji_placeholder_is_simplified_without_inferring_unmapped_names():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" zh_cn="架空麗華" />
      <a jp="架空優華" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華（架空丽华）'
    assert mapping.resolve('架空優華') == '架空優華'


def test_kanji_placeholder_simplification_respects_ownership_conflicts():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" zh_cn="架空麗華" />
      <a jp="其他原名" keyword="架空丽华" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華'


def test_kanji_name_with_missing_chinese_requires_equivalent_keyword():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" keyword="架空丽华,其他旧名" />
      <a jp="架空優華" keyword="其他旧名" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華（架空丽华）'
    assert mapping.resolve('架空優華') == '架空優華'


@pytest.mark.parametrize(('name', 'expected'), [
    ('桜ゆの', '桜ゆの（樱柚乃）'),
    ('三澄寧々', '三澄寧々（三澄宁宁）'),
    ('七嶋舞', '七嶋舞（七岛舞）'),
    ('和香なつき', '和香なつき（和香夏月）'),
    ('めぐり（藤浦めぐ）', 'めぐり（惠理）'),
])
@pytest.mark.parametrize('normalize', [True, False])
def test_reported_names_with_bundled_supplement(reported_mapping, name, expected, normalize):
    assert reported_mapping.resolve(name, normalize_aliases=normalize) == expected
    assert reported_mapping.resolve(expected, normalize_aliases=normalize) == expected


@pytest.mark.parametrize('normalize', [True, False])
def test_reported_names_are_consistent_in_metadata_paths_and_nfo(tmp_path, reported_mapping, normalize):
    cfg = load_config()
    cfg = cfg.model_copy(update={'r18dev': cfg.r18dev.model_copy(update={'normalize_actress_name': normalize})})
    names = ['桜ゆの', '三澄寧々', '七嶋舞', '和香なつき', 'めぐり（藤浦めぐ）']
    expected = ['桜ゆの（樱柚乃）', '三澄寧々（三澄宁宁）', '七嶋舞（七岛舞）', '和香なつき（和香夏月）', 'めぐり（惠理）']
    pictures = {name: f'https://example.test/{index}.jpg' for index, name in enumerate(names)}
    movie = Movie('ABC-123')
    movie.files = [str(tmp_path / 'ABC-123.mp4')]
    source = MovieInfo(movie, title='title', cover='cover', actress=names[:], actress_pics=pictures.copy())
    result = CrawlResult({'r18dev': source}, [], movie.dvdid, None, 'normal')
    merged = MetadataMerger(cfg, aliases=reported_mapping).merge(movie, result)
    assert merged.actress == expected
    assert merged.actress_pics == dict(zip(expected, pictures.values(), strict=True))
    assert source.actress == names and source.actress_pics == pictures
    movie.info = merged
    generate_names(movie, cfg)
    assert ','.join(expected) in movie.save_dir
    target = tmp_path / 'movie.nfo'
    write_nfo(merged, target, cfg)
    tree = etree.parse(str(target))
    assert tree.xpath('/movie/actor/name/text()') == expected
    assert tree.xpath('/movie/actor/thumb/text()') == list(pictures.values())


@pytest.mark.parametrize(('jp', 'keywords', 'expected'), [
    ('桜なな', '樱奈奈', '桜なな（樱奈奈）'),
    ('嶋花舞', '岛花舞', '嶋花舞（岛花舞）'),
    ('架空寧々', '架空宁宁', '架空寧々（架空宁宁）'),
    ('桜なな', '樱奈奈,樱菜菜', '桜なな'),
    ('弁ゆの', '瓣柚乃,辨柚乃', '弁ゆの'),
])
def test_japanese_variant_and_iteration_normalization_is_not_actor_specific(jp, keywords, expected):
    mapping = ActorMapping.from_xml(
        f'<actor-mapping><a jp="{jp}" zh_cn="{jp}" keyword="{keywords}" /></actor-mapping>'.encode()
    )
    assert mapping.resolve(jp) == expected


def test_japanese_placeholder_does_not_hide_valid_traditional_field():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="嶋花舞" zh_cn="嶋花舞" zh_tw="島花舞" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('嶋花舞') == '嶋花舞（島花舞）'


def test_normalized_lookup_and_cross_actor_collisions():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="桜なな" keyword="樱奈奈" />
      <a jp="嶋花ゆな" keyword="嶋花由奈" />
      <a jp="其他演员" zh_cn="岛花由奈" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('櫻なな') == '桜なな（樱奈奈）'
    assert mapping.resolve('嶋花ゆな') == '嶋花ゆな'
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="桜なな" /><a jp="櫻なな" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('樱なな') == '樱なな'


@pytest.mark.parametrize('name', [
    'めぐり（森沢かな）', 'めぐり（未知名字）', '未知名字（藤浦めぐ）',
    'めぐり（共用别名）', 'めぐり（藤浦めぐ（旧名））',
])
def test_composite_name_requires_unique_same_actor_components(name):
    mapping = ActorMapping.from_xml(XML.replace(b'</actor-mapping>', '''
      <a jp="藤浦めぐ" keyword="共用别名" />
      <a jp="其他原名" keyword="共用别名" />
    </actor-mapping>'''.encode()))
    assert mapping.resolve(name) == name


def test_exact_composite_entry_is_not_split():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空（芸名）" zh_cn="明确译名" />
      <a jp="架空" /><a jp="芸名" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空（芸名）') == '架空（芸名）（明确译名）'


def test_explicit_alias_translation_is_distinct_from_main_translation():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空めぐ" zh_cn="架空惠" keyword="めぐり">
        <alias jp="めぐり" zh_cn="惠理" />
      </a>
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空めぐ') == '架空めぐ（架空惠）'
    assert mapping.resolve('めぐり', normalize_aliases=False) == 'めぐり（惠理）'
    assert mapping.resolve('めぐり') == '架空めぐ（架空惠）'
    assert mapping.resolve('めぐり（架空めぐ）') == 'めぐり（惠理）'


@pytest.mark.parametrize('other', [
    '<a jp="架空めぐ"><alias jp="めぐり" zh_cn="另一个译名" /></a>',
    '<a jp="其他原名" keyword="めぐり" />',
])
def test_conflicting_alias_translation_or_ownership_preserves_input(other):
    mapping = ActorMapping.from_xml(f'''<actor-mapping>
      <a jp="架空めぐ" zh_cn="架空惠"><alias jp="めぐり" zh_cn="惠理" /></a>
      {other}
    </actor-mapping>'''.encode())
    assert mapping.resolve('めぐり（架空めぐ）') == 'めぐり（架空めぐ）'
    assert mapping.resolve('めぐり', normalize_aliases=False) == 'めぐり'


def test_explicit_alias_translation_allows_other_actors_with_same_chinese_name():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空めぐ" zh_cn="架空惠"><alias jp="めぐり" zh_cn="惠理" /></a>
      <a jp="其他原名" keyword="恵理" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('めぐり（架空めぐ）') == 'めぐり（惠理）'


def test_supplement_respects_upstream_fields_and_explicit_aliases():
    xml = '''<actor-mapping>
      <a jp="架空なつき" zh_cn="上游中文"><alias jp="旧别名" zh_cn="上游别名中文" /></a>
    </actor-mapping>'''.encode()
    supplement = '''<actor-mapping>
      <a jp="架空なつき" zh_cn="补充中文" source="https://example.test">
        <alias jp="旧别名" zh_cn="补充别名中文" />
      </a>
      <a jp="孤立父名" source="https://example.test"><alias jp="孤立别名" zh_cn="中文" /></a>
    </actor-mapping>'''.encode()
    mapping = ActorMapping.from_xml(xml, supplement=supplement)
    assert mapping.row_count == 1
    assert mapping.resolve('架空なつき') == '架空なつき（上游中文）'
    assert mapping.resolve('旧别名', normalize_aliases=False) == '旧别名（上游别名中文）'
    assert mapping.resolve('孤立别名') == '孤立别名'


@pytest.mark.parametrize('supplement', [
    b'<invalid />',
    '<actor-mapping><a jp="架空名" zh_cn="中文" /></actor-mapping>'.encode(),
    '<actor-mapping><a jp="架空名" zh_cn="中文" source="https://example.test"><alias jp="别名" /></a></actor-mapping>'.encode(),
])
def test_invalid_supplement_is_not_silently_ignored(supplement):
    with pytest.raises(ActorMappingError):
        ActorMapping.from_xml(XML, supplement=supplement)
