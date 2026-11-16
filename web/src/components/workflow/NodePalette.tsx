import { nodeKindLabel } from "../../workflows";

const PALETTE: { type: string; hint: string }[] = [
  { type: "tool", hint: "调用注册表工具" },
  { type: "agent", hint: "自主规划并调用工具" },
  { type: "condition", hint: "按比较结果分支" },
  { type: "review", hint: "人工通过后继续" },
  { type: "output", hint: "汇总结果与产物" },
  { type: "input", hint: "运行入参（最多一个）" },
];

export default function NodePalette({
  hasInput,
  readOnly,
  onAdd,
}: {
  hasInput: boolean;
  readOnly: boolean;
  onAdd: (type: string) => void;
}) {
  return (
    <aside className="card card-pad space-y-2">
      <h2 className="text-sm font-semibold">节点</h2>
      {PALETTE.map((item) => {
        const blocked = readOnly || (item.type === "input" && hasInput);
        return (
          <button
            key={item.type}
            className="w-full rounded-lg border border-line bg-white px-3 py-2 text-left hover:border-sage-500 disabled:opacity-40"
            disabled={blocked}
            onClick={() => onAdd(item.type)}
            type="button"
          >
            <span className="block text-sm text-ink-900">{nodeKindLabel(item.type)}</span>
            <span className="block text-[11px] text-ink-500">{item.hint}</span>
          </button>
        );
      })}
      <p className="pt-2 text-[11px] leading-5 text-ink-500">
        点击加入画布，拖动节点调整位置；选中后按 Backspace 删除。
      </p>
    </aside>
  );
}
