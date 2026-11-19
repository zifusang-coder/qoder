import { useEffect, useState } from "react";
import { EXECUTION_ROLES, TEAM_BUDGET_KEYS, roleLabel } from "../../agentTeam";
import type {
  Graph,
  GraphNode,
  InputField,
  SkillOption,
  ToolSpec,
} from "../../types/workflow";
import {
  inputFields,
  nodeKindLabel,
  variableCandidates,
  type VariableOption,
} from "../../workflows";

const OPERATORS = [
  { value: "equals", label: "等于" },
  { value: "not_equals", label: "不等于" },
  { value: "contains", label: "包含" },
  { value: "not_empty", label: "非空" },
  { value: "gt", label: "大于" },
  { value: "lt", label: "小于" },
];

const FIELD_TYPES = ["string", "number", "boolean"];

function schemaProperties(
  schema: Record<string, unknown> | undefined,
): [string, Record<string, unknown>][] {
  const properties = schema?.properties;
  if (typeof properties !== "object" || properties === null) return [];
  return Object.entries(properties as Record<string, Record<string, unknown>>);
}

function propertyLabel(name: string, spec: Record<string, unknown>): string {
  return typeof spec.title === "string" && spec.title ? spec.title : name;
}

function propertyType(spec: Record<string, unknown>): string {
  return typeof spec.type === "string" ? spec.type : "string";
}

/**
 * 渲染期仅做结构性校验：`key` 允许为空字符串，字段行在用户清空重输时保持挂载。
 * key 非空与 type 白名单的严格规则由 `workflows.ts` 的 `inputFields()` 对消费方把关。
 */
function isInputFieldEntry(value: unknown): value is InputField {
  if (typeof value !== "object" || value === null) return false;
  return typeof (value as Record<string, unknown>).key === "string";
}

function VariablePicker({
  options,
  disabled = false,
  onPick,
}: {
  options: VariableOption[];
  disabled?: boolean;
  onPick: (value: string) => void;
}) {
  return (
    <select
      className="input text-xs"
      disabled={disabled}
      value=""
      onChange={(event) => {
        const value = event.target.value;
        if (value) onPick(value);
      }}
    >
      <option value="">插入变量…</option>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

function appendValue(current: string, value: string): string {
  if (!current.trim()) return value;
  return `${current}${current.endsWith(" ") ? "" : " "}${value}`;
}

function TextAreaWithVariables({
  value,
  rows,
  options,
  ariaLabel,
  disabled = false,
  onChange,
}: {
  value: string;
  rows: number;
  options: VariableOption[];
  ariaLabel: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="space-y-1">
      <textarea
        aria-label={ariaLabel}
        className="input"
        disabled={disabled}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <VariablePicker
        disabled={disabled}
        options={options}
        onPick={(picked) => onChange(appendValue(value, picked))}
      />
    </div>
  );
}

function ArgsForm({
  tool,
  args,
  options,
  skills,
  disabled = false,
  onChange,
}: {
  tool: ToolSpec | null;
  args: Record<string, unknown>;
  options: VariableOption[];
  skills: SkillOption[];
  disabled?: boolean;
  onChange: (args: Record<string, unknown>) => void;
}) {
  const properties = schemaProperties(tool?.parameters);
  const setArg = (key: string, value: unknown) =>
    onChange({ ...args, [key]: value });

  return (
    <div className="space-y-2">
      {properties.length === 0 && (
        <p className="text-[11px] text-ink-500">该工具没有声明参数。</p>
      )}
      {properties.map(([key, spec]) => {
        const type = propertyType(spec);
        const label = propertyLabel(key, spec);
        if (tool?.name === "skill.run" && key === "skill_id") {
          return (
            <label key={key} className="block space-y-1 text-xs text-ink-500">
              <span>{label}</span>
              <select
                className="input text-xs text-ink-900"
                disabled={disabled}
                value={typeof args[key] === "string" ? (args[key] as string) : ""}
                onChange={(event) => setArg(key, event.target.value)}
              >
                <option value="">选择技能</option>
                {skills.map((skill) => (
                  <option key={skill.id} value={skill.id}>
                    {skill.name}（{skill.id}）
                  </option>
                ))}
              </select>
            </label>
          );
        }
        if (type === "boolean") {
          return (
            <label key={key} className="flex items-center gap-2 text-xs text-ink-700">
              <input
                type="checkbox"
                checked={Boolean(args[key])}
                disabled={disabled}
                onChange={(event) => setArg(key, event.target.checked)}
              />
              <span>{label}</span>
            </label>
          );
        }
        return (
          <div key={key} className="space-y-1">
            <label className="block space-y-1 text-xs text-ink-500">
              <span>{label}</span>
              <input
                className="input text-xs text-ink-900"
                disabled={disabled}
                type={type === "number" || type === "integer" ? "number" : "text"}
                value={
                  typeof args[key] === "string" || typeof args[key] === "number"
                    ? String(args[key])
                    : ""
                }
                onChange={(event) => {
                  const raw = event.target.value;
                  const numeric = type === "number" || type === "integer";
                  setArg(
                    key,
                    numeric && raw && !raw.startsWith("{{") ? Number(raw) : raw,
                  );
                }}
              />
            </label>
            <VariablePicker
              disabled={disabled}
              options={options}
              onPick={(picked) => setArg(key, picked)}
            />
          </div>
        );
      })}
    </div>
  );
}

export default function NodeInspector({
  node,
  graph,
  tools,
  skills,
  readOnly,
  onChange,
  onDelete,
}: {
  node: GraphNode | null;
  graph: Graph;
  tools: ToolSpec[];
  skills: SkillOption[];
  readOnly: boolean;
  onChange: (patch: Record<string, unknown>) => void;
  onDelete: () => void;
}) {
  const [label, setLabel] = useState("");
  const [toolsDraft, setToolsDraft] = useState("");

  const nodeData = node?.data ?? {};
  const nodeId = node?.id;
  const committedLabel = node ? String(node.data.label ?? "") : "";
  const toolsWanted = Array.isArray(nodeData.tools)
    ? (nodeData.tools as unknown[]).filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const teamRoles = Array.isArray(nodeData.roles)
    ? (nodeData.roles as unknown[]).filter(
        (item): item is string => typeof item === "string",
      )
    : [];

  useEffect(() => {
    setLabel(committedLabel);
  }, [nodeId, committedLabel]);

  useEffect(() => {
    setToolsDraft(toolsWanted.join(", "));
    // 仅在切换选中节点时重置草稿，输入过程中的逗号不能被回写覆盖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  if (!node) {
    return (
      <aside className="card card-pad text-sm text-ink-500">
        选中画布上的节点以编辑属性。
      </aside>
    );
  }

  const options = variableCandidates(graph, node.id, inputFields(graph));
  const fields = Array.isArray(nodeData.fields)
    ? nodeData.fields.filter(isInputFieldEntry)
    : [];
  const budget =
    typeof nodeData.budget === "object" && nodeData.budget !== null
      ? (nodeData.budget as Record<string, unknown>)
      : {};
  const args =
    typeof nodeData.args === "object" && nodeData.args !== null
      ? (nodeData.args as Record<string, unknown>)
      : {};
  const selectedTool =
    tools.find((tool) => tool.name === nodeData.tool) ?? null;

  const commitTools = () => {
    onChange({
      tools: toolsDraft
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    });
  };

  return (
    <aside className="card card-pad space-y-3 overflow-y-auto">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{nodeKindLabel(node.type)}节点</h2>
        {!readOnly && (
          <button className="text-xs text-wine-700" onClick={onDelete} type="button">
            删除
          </button>
        )}
      </div>

      <label className="block space-y-1 text-xs text-ink-500">
        <span>显示名称</span>
        <input
          className="input text-sm text-ink-900"
          disabled={readOnly}
          maxLength={80}
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          onBlur={() => {
            if (label !== String(nodeData.label ?? "")) onChange({ label });
          }}
        />
      </label>

      {node.type === "input" && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-ink-500">入参字段</span>
            {!readOnly && (
              <button
                className="text-xs text-sage-700"
                type="button"
                onClick={() =>
                  onChange({
                    fields: [
                      ...fields,
                      { key: `field_${fields.length + 1}`, type: "string" },
                    ],
                  })
                }
              >
                + 字段
              </button>
            )}
          </div>
          {fields.map((field, index) => (
            <div key={index} className="space-y-1 rounded-lg border border-line p-2">
              <input
                className="input text-xs"
                disabled={readOnly}
                placeholder="key（小写字母开头）"
                value={field.key}
                onChange={(event) =>
                  onChange({
                    fields: fields.map((item, position) =>
                      position === index ? { ...item, key: event.target.value } : item,
                    ),
                  })
                }
              />
              <input
                className="input text-xs"
                disabled={readOnly}
                placeholder="显示名"
                value={field.label ?? ""}
                onChange={(event) =>
                  onChange({
                    fields: fields.map((item, position) =>
                      position === index ? { ...item, label: event.target.value } : item,
                    ),
                  })
                }
              />
              <div className="flex items-center gap-2 text-xs">
                <select
                  className="input text-xs"
                  disabled={readOnly}
                  value={field.type}
                  onChange={(event) =>
                    onChange({
                      fields: fields.map((item, position) =>
                        position === index
                          ? { ...item, type: event.target.value as InputField["type"] }
                          : item,
                      ),
                    })
                  }
                >
                  {FIELD_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
                <label className="flex items-center gap-1 text-ink-700">
                  <input
                    type="checkbox"
                    disabled={readOnly}
                    checked={Boolean(field.required)}
                    onChange={(event) =>
                      onChange({
                        fields: fields.map((item, position) =>
                          position === index
                            ? { ...item, required: event.target.checked }
                            : item,
                        ),
                      })
                    }
                  />
                  必填
                </label>
                {!readOnly && (
                  <button
                    className="ml-auto text-wine-700"
                    type="button"
                    onClick={() =>
                      onChange({
                        fields: fields.filter((_, position) => position !== index),
                      })
                    }
                  >
                    删除
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {node.type === "tool" && (
        <div className="space-y-2">
          <label className="block space-y-1 text-xs text-ink-500">
            <span>工具</span>
            <select
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={typeof nodeData.tool === "string" ? nodeData.tool : ""}
              onChange={(event) =>
                onChange({ tool: event.target.value, args: {} })
              }
            >
              <option value="">选择工具</option>
              {tools.map((tool) => (
                <option key={tool.name} value={tool.name}>
                  {tool.name} · {tool.risk}
                </option>
              ))}
            </select>
          </label>
          {selectedTool && (
            <p className="text-[11px] text-ink-500">{selectedTool.description}</p>
          )}
          <ArgsForm
            tool={selectedTool}
            args={args}
            options={options}
            skills={skills}
            disabled={readOnly}
            onChange={(next) => onChange({ args: next })}
          />
        </div>
      )}

      {node.type === "agent" && (
        <div className="space-y-2">
          <div className="space-y-1 text-xs text-ink-500">
            <span>目标</span>
            <TextAreaWithVariables
              value={typeof nodeData.goal === "string" ? nodeData.goal : ""}
              rows={5}
              options={options}
              ariaLabel="目标"
              disabled={readOnly}
              onChange={(goal) => onChange({ goal })}
            />
          </div>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>工具白名单（逗号分隔，支持前缀.*；留空为全部）</span>
            <input
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={toolsDraft}
              onChange={(event) => setToolsDraft(event.target.value)}
              onBlur={commitTools}
              onKeyDown={(event) => {
                if (event.key === "Enter") commitTools();
              }}
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            {(["max_steps", "max_tokens", "max_tool_calls", "max_seconds"] as const).map(
              (key) => (
                <label key={key} className="block space-y-1 text-xs text-ink-500">
                  <span>{key}</span>
                  <input
                    className="input text-xs text-ink-900"
                    disabled={readOnly}
                    min={1}
                    type="number"
                    value={
                      typeof budget[key] === "number" ? String(budget[key]) : ""
                    }
                    onChange={(event) => {
                      const raw = event.target.value;
                      const next: Record<string, unknown> = { ...budget };
                      if (raw === "") {
                        delete next[key];
                      } else {
                        next[key] = Number(raw);
                      }
                      onChange({ budget: next });
                    }}
                  />
                </label>
              ),
            )}
          </div>
        </div>
      )}

      {node.type === "team" && (
        <div className="space-y-2">
          <div className="space-y-1 text-xs text-ink-500">
            <span>目标</span>
            <TextAreaWithVariables
              value={typeof nodeData.goal === "string" ? nodeData.goal : ""}
              rows={5}
              options={options}
              ariaLabel="目标"
              disabled={readOnly}
              onChange={(goal) => onChange({ goal })}
            />
          </div>
          <div className="space-y-1 text-xs text-ink-500">
            <span>参与角色</span>
            <div className="flex flex-wrap gap-3">
              {EXECUTION_ROLES.map((role) => (
                <label key={role} className="flex items-center gap-1 text-xs text-ink-700">
                  <input
                    type="checkbox"
                    checked={role === "creator" || teamRoles.includes(role)}
                    disabled={readOnly || role === "creator"}
                    onChange={(event) =>
                      onChange({
                        roles: event.target.checked
                          ? [...teamRoles, role]
                          : teamRoles.filter((item) => item !== role),
                      })
                    }
                  />
                  {roleLabel(role)}
                  {role === "creator" && <span className="text-ink-500">（必选）</span>}
                </label>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            {TEAM_BUDGET_KEYS.map((key) => (
              <label key={key} className="block space-y-1 text-xs text-ink-500">
                <span>{key}</span>
                <input
                  className="input text-xs text-ink-900"
                  disabled={readOnly}
                  min={1}
                  type="number"
                  value={
                    typeof budget[key] === "number" ? String(budget[key]) : ""
                  }
                  onChange={(event) => {
                    const raw = event.target.value;
                    const next: Record<string, unknown> = { ...budget };
                    if (raw === "") {
                      delete next[key];
                    } else {
                      next[key] = Number(raw);
                    }
                    onChange({ budget: next });
                  }}
                />
              </label>
            ))}
          </div>
        </div>
      )}

      {node.type === "condition" && (
        <div className="space-y-2">
          <div className="space-y-1 text-xs text-ink-500">
            <span>左值</span>
            <TextAreaWithVariables
              value={typeof nodeData.left === "string" ? nodeData.left : ""}
              rows={2}
              options={options}
              ariaLabel="左值"
              disabled={readOnly}
              onChange={(left) => onChange({ left })}
            />
          </div>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>运算符</span>
            <select
              className="input text-xs text-ink-900"
              disabled={readOnly}
              value={
                typeof nodeData.operator === "string"
                  ? nodeData.operator
                  : "contains"
              }
              onChange={(event) => onChange({ operator: event.target.value })}
            >
              {OPERATORS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          {nodeData.operator !== "not_empty" && (
            <div className="space-y-1 text-xs text-ink-500">
              <span>右值</span>
              <TextAreaWithVariables
                value={typeof nodeData.value === "string" ? nodeData.value : ""}
                rows={2}
                options={options}
                ariaLabel="右值"
                disabled={readOnly}
                onChange={(value) => onChange({ value })}
              />
            </div>
          )}
        </div>
      )}

      {node.type === "review" && (
        <div className="space-y-2">
          <label className="block space-y-1 text-xs text-ink-500">
            <span>审批标题</span>
            <input
              className="input text-xs text-ink-900"
              disabled={readOnly}
              maxLength={200}
              value={typeof nodeData.title === "string" ? nodeData.title : ""}
              onChange={(event) => onChange({ title: event.target.value })}
            />
          </label>
          <label className="block space-y-1 text-xs text-ink-500">
            <span>给审批人的说明</span>
            <textarea
              className="input text-xs"
              disabled={readOnly}
              rows={3}
              value={typeof nodeData.note === "string" ? nodeData.note : ""}
              onChange={(event) => onChange({ note: event.target.value })}
            />
          </label>
        </div>
      )}

      {(node.type === "tool" || node.type === "agent" || node.type === "team") && (
        <label className="block space-y-1 text-xs text-ink-500">
          <span>失败重试（0–3）</span>
          <select
            className="input text-xs text-ink-900"
            disabled={readOnly}
            value={Number(nodeData.retry) || 0}
            onChange={(event) => onChange({ retry: Number(event.target.value) })}
          >
            {[0, 1, 2, 3].map((count) => (
              <option key={count} value={count}>
                {count}
              </option>
            ))}
          </select>
        </label>
      )}
    </aside>
  );
}
