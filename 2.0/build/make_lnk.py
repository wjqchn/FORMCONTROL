# -*- coding: utf-8 -*-
"""纯 Python 生成 Windows .lnk 快捷方式（不依赖 COM / pywin32）。

要点（对照真实可用的系统 .lnk 修正）：
1. CLSID 必须是 CLSID_ShellLink = {00021401-0000-0000-C000-000000000046}
   （小端 16 字节：0114020000000000C000000000000046），写错会被 Shell 直接拒绝。
2. 必须含 HasTargetIdList（0x01）。真实 Windows 快捷方式都带 IDList；缺失会让
   Shell 判定为"无关联应用"（WinError 1155）。IDList 用 ILCreateFromPathW 取得。
3. 同时保留 LinkInfo（含绝对 LocalBasePath 的 ANSI + Unicode），与系统 .lnk 一致，
   提高解析健壮性；VolumeID 最小结构、空卷标，偏移精确排布避免混淆。
"""
import base64
import ctypes
import os
import struct
import ctypes.wintypes as wt

NAME = base64.b64decode("6LSo6YeP5paH5o6n5bmz5Y+w").decode("utf-8")   # 质量文控平台
REL = base64.b64decode("RGVza3RvcFzooajmoLzmlbTnkIZcMi4wXGRpc3Q=").decode("utf-8")  # Desktop\表格整理\2.0\dist

BASE = os.path.join(os.environ["USERPROFILE"], REL)
LNK = os.path.join(os.environ["USERPROFILE"], "Desktop", NAME + ".lnk")
TARGET = os.path.join(BASE, NAME + ".exe")


def u32(v):
    return struct.pack("<I", v)


def u16(v):
    return struct.pack("<H", v)


def utf16le(s):
    return s.encode("utf-16-le") + b"\x00\x00"


# ---- 通过 ILCreateFromPathW 取得目标的 ITEMIDLIST（IDList 内容）----
def get_pidl(target):
    dll = ctypes.windll.shell32
    fn = dll.ILCreateFromPathW
    fn.argtypes = [wt.LPCWSTR]
    fn.restype = ctypes.c_void_p
    dll.ILFree.argtypes = [ctypes.c_void_p]
    dll.ILFree.restype = None
    ptr = fn(target)
    if not ptr:
        raise RuntimeError("ILCreateFromPathW 返回 NULL")
    # 遍历 item ID 计算长度（每项 2 字节大小 + 数据，末尾 2 字节 0 终止符）
    offset = 0
    total = 0
    for _ in range(4096):
        size = struct.unpack("<H", ctypes.string_at(ptr + offset, 2))[0]
        if size == 0:
            total = offset + 2
            break
        offset += size
    else:
        raise RuntimeError("PIDL 遍历超限，可能异常")
    data = ctypes.string_at(ptr, total)
    dll.ILFree(ptr)
    return data


pidl = get_pidl(TARGET)
idlist = u16(len(pidl)) + pidl   # IDListSize + ItemIDList

# ---- ShellLinkHeader (76 字节) ----
LINK_CLSID = bytes.fromhex("0114020000000000C000000000000046")  # CLSID_ShellLink
link_flags = 0x0001 | 0x0002 | 0x0010 | 0x4000  # HasTargetIdList | HasLinkInfo | HasWorkingDir | IsUnicode
header = b""
header += u32(0x0000004C)          # HeaderSize = 76
header += LINK_CLSID                # LinkCLSID (16)
header += u32(link_flags)          # LinkFlags
header += u32(0x00000020)          # FileAttributes = FILE_ATTRIBUTE_NORMAL
header += b"\x00" * 8              # CreationTime
header += b"\x00" * 8              # AccessTime
header += b"\x00" * 8              # WriteTime
header += u32(0)                   # FileSize
header += u32(0)                   # IconIndex
header += u32(0x00000001)          # ShowCommand = SW_SHOWNORMAL
header += u16(0)                   # HotKey
header += u16(0)                   # Reserved1
header += u32(0)                   # Reserved2
header += u32(0)                   # Reserved3
assert len(header) == 76, len(header)

# ---- LinkInfo（含绝对 LocalBasePath，ANSI + Unicode）----
volume_id = u32(16) + u32(3) + u32(0) + u32(16)  # size=16, DriveType=3(FIXED), serial=0, VolumeLabelOffset=16
vol_label = b"\x00"                              # 空 ANSI 卷标（1 字节 null）

ansi_target = TARGET.encode("gbk", "replace") + b"\x00"
uni_target = utf16le(TARGET)
ansi_suffix = b"\x00"   # 空 CommonPathSuffix
uni_suffix = b"\x00\x00"

HDR = 36
off_volume = HDR                                                  # 36
off_local = HDR + len(volume_id) + len(vol_label)                 # 36+16+1 = 53
off_csuffix = off_local + len(ansi_target)                        # 53 + len(ansi_target)
off_local_u = off_csuffix + len(ansi_suffix)                      # +1
off_csuffix_u = off_local_u + len(uni_target)
linkinfo_size = off_csuffix_u + len(uni_suffix)

linkinfo = b""
linkinfo += u32(linkinfo_size)        # LinkInfoSize
linkinfo += u32(HDR)                   # LinkInfoHeaderSize = 36（含两个 Unicode 偏移字段）
linkinfo += u32(0x00000001)            # LinkInfoFlags = VolumeIDAndLocalBasePath
linkinfo += u32(off_volume)            # VolumeIDOffset
linkinfo += u32(off_local)             # LocalBasePathOffset
linkinfo += u32(0)                     # CommonNetworkRelativeLinkOffset = 0（无）
linkinfo += u32(off_csuffix)           # CommonPathSuffixOffset
linkinfo += u32(off_local_u)           # LocalBasePathOffsetUnicode
linkinfo += u32(off_csuffix_u)         # CommonPathSuffixOffsetUnicode
linkinfo += volume_id
linkinfo += vol_label
linkinfo += ansi_target
linkinfo += ansi_suffix
linkinfo += uni_target
linkinfo += uni_suffix
assert len(linkinfo) == linkinfo_size, (len(linkinfo), linkinfo_size)

# ---- STRING_DATA（Unicode）：仅 WORKING_DIR ----
working_dir = u16(len(BASE) + 1) + utf16le(BASE)

out = header + idlist + linkinfo + working_dir

os.makedirs(os.path.dirname(LNK), exist_ok=True)
with open(LNK, "wb") as f:
    f.write(out)

print("WROTE", LNK, "bytes", len(out))
print("TARGET", TARGET, "exists", os.path.isfile(TARGET))
print("LNK exists", os.path.isfile(LNK))
print("IDList bytes", len(idlist), "PIDL items total", len(pidl))
print("LinkInfo bytes", linkinfo_size)
