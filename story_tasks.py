"""Create editable AECPP cartoons from text, or learn reviewed directing examples."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="小说 / 剧本生成可编辑 AECPP 动画工程")
    commands = parser.add_subparsers(dest="command", required=True)
    oneclick = commands.add_parser("oneclick", help="自动匹配现有素材并生成独立 AECPP 工程")
    oneclick.add_argument("source", type=Path)
    oneclick.add_argument("--project", required=True, type=Path)
    oneclick.add_argument("--resources", type=Path, help="首次可指定；以后复用已保存目录或自动发现同级 AECPP")
    oneclick.add_argument("--title", default=None)
    oneclick.add_argument("--format", choices=("auto", "novel", "script", "fountain"), default="auto")
    oneclick.add_argument('--with-sound',action='store_true',help='自动离线配音、口型同步和喜剧音效（Windows 中文语音）')
    generate = commands.add_parser("generate", help="文本生成分镜和 AECPP 工程")
    generate.add_argument("source", type=Path)
    generate.add_argument("--output", required=True, type=Path, help="新的 .aecpp 工程路径")
    generate.add_argument("--project", type=Path, help="读取此映格工程已确认的编排偏好及自动字幕时长参考")
    generate.add_argument("--title", default=None)
    generate.add_argument("--format", choices=("auto", "novel", "script", "fountain"), default="auto")
    generate.add_argument("--board", type=Path, help="另存可继续修改的分镜 JSON")
    generate.add_argument("--actor-script", action="store_true", help="输出 AECPP 角色剧本 JSON，用于绑定自己的角色与配音")
    export = commands.add_parser("export", help="将已修改的分镜导出为 AECPP 工程")
    export.add_argument("board", type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--actor-script", action="store_true")
    for command in (generate,export):
        command.add_argument("--resources",type=Path,help="AECPP 现有素材根目录；组装真实角色、动作、表情与背景")
        command.add_argument("--bindings",type=Path,help="已保存的素材映射 JSON，可由小说成片的素材页保存")
    learn = commands.add_parser("learn", help="将已审阅分镜加入工程的编排学习")
    learn.add_argument("boards", type=Path, nargs="+")
    learn.add_argument("--project", required=True, type=Path)
    args = parser.parse_args(argv)
    from story_pipeline import generate_project_storyboard, load_storyboard, save_storyboard, train_style, reference_description
    from aecpp_export import export_project, export_script
    report = lambda message: print(str(message), file=sys.stderr, flush=True)
    try:
        if args.command == "oneclick":
            from story_automation import generate_animation
            from subtitles import decode_text
            if args.source.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("文本文件超过 2 MiB，请按章节拆分")
            source_format = "fountain" if args.format == "auto" and args.source.suffix.lower() == ".fountain" else args.format
            generated = generate_animation(args.project, decode_text(args.source.read_bytes()),
                args.title or args.source.stem, source_format=source_format, resource_root=args.resources, report=report,sound=args.with_sound)
            result = {"project": str(generated["output"]), "directory": str(generated["run_dir"]),
                      "report": str(generated["report_path"]), "shots": len(generated["board"]["shots"]),
                      "warnings": generated["warnings"], "note": "使用现有素材生成；请保留同目录的 story.assets"}
            if generated.get('sound'):result['sound']=generated['sound']
        elif args.command == "learn":
            result = train_style(args.project, [load_storyboard(path) for path in args.boards], report=report)
        else:
            if args.command == "generate":
                if args.source.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("文本文件超过 2 MiB，请按章节拆分")
                if args.output.resolve() == args.source.resolve():
                    raise ValueError("输出不能覆盖原始文本")
                if args.output.exists():
                    raise ValueError("输出工程已存在，请选择新的文件名")
                if args.board and (args.board.exists() or args.board.resolve() in {args.source.resolve(), args.output.resolve()}):
                    raise ValueError("分镜保存位置已存在或与输入 / 工程路径冲突")
                from subtitles import decode_text
                text = decode_text(args.source.read_bytes())
                source_format = "fountain" if args.format == "auto" and args.source.suffix.lower() == ".fountain" else args.format
                board = generate_project_storyboard(args.project, text, args.title or args.source.stem, source_format=source_format)
                if reference_description(board):
                    report(reference_description(board))
            else:
                if args.output.resolve() == args.board.resolve():
                    raise ValueError("输出不能覆盖分镜源文件")
                board = load_storyboard(args.board)
            if args.resources:
                if args.actor_script:
                    raise ValueError("素材组装输出 .aecpp；请不要同时使用 --actor-script")
                from aecpp_resources import scan_resources
                from aecpp_resource_export import export_resource_project
                catalog = scan_resources(args.resources,report=report)
                bindings = None
                if args.bindings:
                    if args.bindings.stat().st_size>1024*1024:
                        raise ValueError("素材映射文件过大")
                    bindings = json.loads(args.bindings.read_text(encoding="utf-8"))
                output = export_resource_project(board,args.output,catalog=catalog,bindings=bindings,report=report)
            else:
                if args.bindings:
                    raise ValueError("使用 --bindings 时请同时指定 --resources")
                output = (export_script if args.actor_script else export_project)(board, args.output, report=report)
            if args.command == "generate" and args.board:
                save_storyboard(board, args.board)
            result = {"project": str(output), "shots": len(board["shots"]),
                      "duration": round(sum(shot["duration"] for shot in board["shots"]), 3),
                      "note": ("现有素材已组装；请保留工程旁的同名 .assets 素材目录" if args.resources else
                               "在 AECPP 剧本工作台导入，绑定角色和配音后制作动画" if args.actor_script else
                               "可编辑图层、动作关键帧与字幕；配音和人物素材可在 AECPP 中继续制作")}
            if "reference_summary" in board:
                result["reference_summary"] = board["reference_summary"]
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report(f"未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
