"""从arzon抓取Image Video数据（委托给arzon模块）"""

from javsp.datatype import MovieInfo
from javsp.web.arzon import parse_data as _parse_data


def parse_data(movie: MovieInfo):
    """解析指定番号的Image Video影片数据"""
    _parse_data(movie, mode="arzon_iv")
