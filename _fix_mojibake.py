#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性修复 grok_register_ttk.py 里 17 处乱码中文字符串（YYDS/DuckMail/取消注册）。
用法：  python _fix_mojibake.py
跑完确认无误后可删除本文件。
"""
import io
import py_compile

PATH = "grok_register_ttk.py"

# 行号 -> 正确的语句体（缩进会自动沿用原行）
FIXES = {
    650: 'raise RegistrationCancelled("用户停止注册")',
    839: 'raise Exception(f"YYDS 创建邮箱失败: {data}")',
    893: 'raise Exception(f"YYDS 获取邮件详情失败: {data}")',
    904: 'raise Exception("YYDS 没有返回任何可用域名")',
    914: 'raise Exception("YYDS 无已验证域名可用")',
    932: 'raise Exception("获取 YYDS token 失败")',
    933: 'print(f"[*] 已创建 YYDS 邮箱: {address}")',
    954: 'log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")',
    969: 'log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")',
    981: 'log_callback(f"[Debug] YYDS 收到邮件: {subject}")',
    985: 'log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")',
    1175: 'raise Exception("DuckMail 没有返回任何可用域名")',
    1183: 'raise Exception("DuckMail 无已验证域名可用")',
    1232: 'raise Exception("获取 DuckMail token 失败")',
    1322: 'log_callback(f"[Debug] 拉取邮件列表失败: {exc}")',
    1337: 'log_callback(f"[Debug] 获取邮件详情失败: {exc}")',
    1349: 'log_callback(f"[Debug] 收到邮件: {subject}")',
    1353: 'log_callback(f"[*] 从邮件中提取到验证码: {code}")',
}


def is_mojibake(s):
    # 乱码行含有非 ASCII 且不含正常中文标点/常用字之外的怪异 CJK；这里用简单守卫：
    # 目标行应包含非 ASCII 字符（旧乱码）。若已是正常中文也无妨，替换为等价正确文案。
    return any(ord(ch) > 0x7F for ch in s)


def main():
    with io.open(PATH, encoding="utf-8") as f:
        lines = f.readlines()

    changed = 0
    for ln, body in FIXES.items():
        idx = ln - 1
        if idx < 0 or idx >= len(lines):
            print(f"[skip] 行 {ln} 越界，可能文件已变动，请核对后手动修改")
            continue
        orig = lines[idx]
        if not is_mojibake(orig):
            print(f"[skip] 行 {ln} 无非 ASCII 内容，疑似行号漂移，跳过：{orig.strip()[:40]}")
            continue
        indent = orig[: len(orig) - len(orig.lstrip())]
        lines[idx] = indent + body + "\n"
        changed += 1

    with io.open(PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"已修复 {changed} 行乱码")

    py_compile.compile(PATH, doraise=True)
    print("py_compile 通过")


if __name__ == "__main__":
    main()
