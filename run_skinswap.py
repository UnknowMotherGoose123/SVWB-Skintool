"""exe 入口（PyInstaller 打包用），也可以直接 python run_skinswap.py 运行。"""
import multiprocessing

from svwb_skinswap.server import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
