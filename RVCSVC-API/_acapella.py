"""清唱（acapella）生成：inst_vol 压到 -100 触发后端清唱模式，完全不叠伴奏。

用法：python _acapella.py <歌曲ID> <模型名> <半音数> <输出文件名>
"""
import os
import shutil
import sys
import time

os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

from gradio_client import Client

song_id = sys.argv[1]
model = sys.argv[2]
key_shift = int(sys.argv[3])
out_name = sys.argv[4]
OUT_DIR = r"D:\workplace\2026-10-26-13-11-24\output"

c = Client("http://127.0.0.1:3333/", verbose=False)
t0 = time.time()
r = c.predict(
    song_name_src=song_id, key_shift=key_shift, vocal_vol=0, inst_vol=0,
    model_dropdown=model, acapella="true",
    reverb_intensity=2, delay_intensity=0,
    uvr5_agg=10, uvr5_tta=False, uvr5_postprocess=False,
    uvr5_window_size=512, uvr5_high_end_process="mirroring",
    msst_batch_size=1, msst_num_overlap=4, msst_normalize=False,
    vocal_postprocess=False, shift_accompaniment=True,
    f0_method="rmvpe", index_rate="0.75", filter_radius=3,
    api_name="/convert",
)
if r and os.path.isfile(r):
    dst = os.path.join(OUT_DIR, out_name)
    shutil.copy2(r, dst)
    print("OK %.1fs -> %s (%.1f MiB)" % (time.time() - t0, dst, os.path.getsize(dst) / 1048576))
else:
    print("FAILED ->", r)
