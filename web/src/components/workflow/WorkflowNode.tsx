import { Handle, Position, type NodeProps } from "@xyflow/react";
import { memo } from "react";
import { nodeKindLabel, nodeStatusBadge, nodeStatusLabel } from "../../workflows";

function WorkflowNodeView({ data, selected }: NodeProps) {
  const kind = typeof data.kind === "string" ? data.kind : "";
  const label = typeof data.label === "string" ? data.label : "";
  const status = typeof data.status === "string" ? data.status : "";
  const condition = kind === "condition";
  return (
    <div
      className={[
        "relative min-w-[150px] rounded-lg border bg-surface px-3 py-2 text-xs shadow-sm",
        selected ? "border-brand-500 ring-2 ring-brand-100" : "border-line",
        status === "skipped" ? "opacity-50" : "",
        status === "running" ? "border-brand-500" : "",
        status === "error" || status === "denied" ? "border-coral-500" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium text-ink-900">{label}</span>
        {status && <span className={nodeStatusBadge(status)}>{nodeStatusLabel(status)}</span>}
      </div>
      <div className="mt-0.5 text-[10px] text-ink-500">{nodeKindLabel(kind)}</div>
      {kind !== "input" && <Handle type="target" position={Position.Left} />}
      {condition ? (
        <>
          <Handle
            id="true"
            type="source"
            position={Position.Right}
            style={{ top: "35%" }}
          />
          <Handle
            id="false"
            type="source"
            position={Position.Right}
            style={{ top: "70%" }}
          />
          <span className="absolute right-1 top-[22%] text-[9px] text-brand-700">是</span>
          <span className="absolute right-1 top-[57%] text-[9px] text-coral-700">否</span>
        </>
      ) : (
        kind !== "output" && <Handle type="source" position={Position.Right} />
      )}
    </div>
  );
}

export default memo(WorkflowNodeView);
