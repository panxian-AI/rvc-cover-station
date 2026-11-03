"""照抄插件 main.py 的真实参数构造方式调一次 /convert，用于改完后端代码后自检。

用法：
    D:\\RVCSVC-API\\apienv\\Scripts\\python.exe 自检_模拟插件调用.py [歌曲ID] [模型名]

不带参数默认用 2658660685 / deepseek.pth。
"""
import os
import sys
import time
import traceback

os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

from gradio_client import Client

song_id = sys.argv[1] if len(sys.argv) > 1 else "2658660685"
model = sys.argv[2] if len(sys.argv) > 2 else "deepseek.pth"

c = Client("http://127.0.0.1:3333/", verbose=False)
t0 = time.time()
try:
    # 关键：index_rate 故意传字符串，和插件 _conf_schema.json 的 string 类型保持一致。
    # 后端若把它绑在 gr.Slider 上，会崩在 'str' < 'int'，函数体根本进不去。
    r = c.predict(
        song_name_src=song_id, key_shift=-6, vocal_vol=0, inst_vol=0,
        model_dropdown=model,
        reverb_intensity=4, delay_intensity=0,
        uvr5_agg=10, uvr5_tta=False, uvr5_postprocess=False,
        uvr5_window_size=512, uvr5_high_end_process="mirroring",
        msst_batch_size=1, msst_num_overlap=4, msst_normalize=False,
        vocal_postprocess=False, shift_accompaniment=True,
        f0_method="rmvpe", index_rate="0.75", filter_radius=3,
        api_name="/convert",
    )
    print("RESULT:", r)
    if r and os.path.isfile(r):
        print("成功，成品 %.1f MiB" % (os.path.getsize(r) / 1048576))
    else:
        print("警告：返回的路径不存在")
except Exception:
    print("FAILED")
    traceback.print_exc()
print("耗时 %.1fs" % (time.time() - t0))
