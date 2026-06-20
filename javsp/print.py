"""改写内置的print函数，将其输出重定向到tqdm"""

import inspect

import tqdm

__all__ = ["TqdmOut", "install"]


# 普通输出和tqdm的输出混在一起会导致显示错乱，故在使用tqdm时要使用tqdm.write方法。
# 但是不希望又每个模块都使用tqdm.write方法，这样会显得混乱而且会导致与tqdm强耦合。
# 这个模块被设计来解决上面的问题：导入此模块后，全局覆盖内置的print，将输出都重定向到tqdm。
# install() 只在项目入口显式调用，这将改写所有后续导入的模块中的print的行为；
# 在单个模块内，不执行 install，这样的话在各个模块内仍然可以直接使用print

builtin_print = print


def flex_print(*args, **kwargs):
    try:
        tqdm.tqdm.write(*args, **kwargs)
    except Exception:
        builtin_print(*args, **kwargs)


def install():
    """将内置 print 替换为与 tqdm 协同的版本"""
    if not getattr(inspect.builtins, "__javsp_print_patched", False):
        inspect.builtins.print = flex_print
        inspect.builtins.__javsp_print_patched = True


class TqdmOut:
    """用于将logging的stream输出重定向到tqdm"""

    @classmethod
    def write(cls, s, file=None, nolock=False):
        tqdm.tqdm.write(s, file=file, end="", nolock=nolock)
