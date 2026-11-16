/** plan-flow-redesign：子代理 Q 版角色头像——共享 Dada.tsx 骨架常量，不复制底层 SVG 路径。 */
import { memo, useId } from "react";
import { DADA_BODY, DADA_EYE, DADA_TUFT } from "../mascot/Dada";

export type SubagentRole = "explore" | "coder" | "operator" | "verifier";

export const SUBAGENT_ROLE_LABEL: Record<SubagentRole, string> = {
  explore: "探索嗒",
  coder: "编码嗒",
  operator: "操控嗒",
  verifier: "校验嗒",
};

export function resolveSubagentRole(opts: {
  name: string;
  workflowKind?: string | null;
  roleArgs?: { mode?: string; explore?: boolean; purpose?: string; agent_type?: string };
}): SubagentRole {
  const { name, workflowKind, roleArgs } = opts;
  if (roleArgs?.purpose === "operator") return "operator";
  if (workflowKind === "check" || workflowKind === "review") return "verifier";
  if (
    roleArgs?.explore === true
    || roleArgs?.mode === "readonly"
    || roleArgs?.agent_type === "explore"
    || workflowKind === "explore"
    || /^探索[·:：\s]/.test(name)
  ) {
    return "explore";
  }
  return "coder";
}

const TONE_END: Record<SubagentRole, string> = {
  explore: "#d5e6f8",
  coder: "#cceee8",
  operator: "#dce0fb",
  verifier: "#d2f1dc",
};

export const SubagentAvatar = memo(function SubagentAvatar({
  role,
  running = false,
}: {
  role: SubagentRole;
  running?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  const gradId = `saGrad${uid}`;

  return (
    <div className={`sa-avatar tone-${role}${running ? " is-running" : ""}`} aria-hidden>
      <svg width="22" height="22" viewBox="0 0 120 120">
        <defs>
          <radialGradient id={gradId} cx="40%" cy="32%" r="78%">
            <stop offset="0" stopColor="#ffffff" />
            <stop offset="0.62" stopColor="#f3f5fb" />
            <stop offset="1" stopColor={TONE_END[role]} />
          </radialGradient>
        </defs>

        {role === "explore" && (
          <>
            <path d={DADA_TUFT} fill="#38bdf8" />
            <path d={DADA_BODY} fill={`url(#${gradId})`} />
            <g fill="#ff8fa8" opacity="0.4">
              <ellipse cx="35" cy="77" rx="7" ry="4" />
              <ellipse cx="85" cy="77" rx="7" ry="4" />
            </g>
            <ellipse cx="48" cy="62" rx="7" ry="10.4" fill={DADA_EYE} />
            <ellipse cx="72" cy="62" rx="7" ry="10.4" fill={DADA_EYE} />
            <circle cx="88" cy="28" r="9" fill="none" stroke="#0284c7" strokeWidth="4.5" />
            <line x1="94" y1="34" x2="103" y2="43" stroke="#0284c7" strokeWidth="4.5" strokeLinecap="round" />
          </>
        )}

        {role === "coder" && (
          <>
            <path d={DADA_TUFT} fill="#0d9488" />
            <path d={DADA_BODY} fill={`url(#${gradId})`} />
            <rect x="33" y="49" width="24" height="16" rx="7" fill="none" stroke="#0d9488" strokeWidth="4" />
            <rect x="63" y="49" width="24" height="16" rx="7" fill="none" stroke="#0d9488" strokeWidth="4" />
            <line x1="57" y1="57" x2="63" y2="57" stroke="#0d9488" strokeWidth="3.5" />
            <ellipse cx="45" cy="60" rx="5.5" ry="8" fill={DADA_EYE} />
            <ellipse cx="75" cy="60" rx="5.5" ry="8" fill={DADA_EYE} />
            <g fill="#ff8fa8" opacity="0.38">
              <ellipse cx="34" cy="78" rx="6.5" ry="3.8" />
              <ellipse cx="86" cy="78" rx="6.5" ry="3.8" />
            </g>
          </>
        )}

        {role === "operator" && (
          <>
            <rect x="8" y="52" width="11" height="24" rx="5" fill="#6366f1" />
            <rect x="101" y="52" width="11" height="24" rx="5" fill="#6366f1" />
            <path d="M16 54 C18 22 40 16 60 16 C80 16 102 22 104 54" fill="none" stroke="#6366f1" strokeWidth="4.5" strokeLinecap="round" />
            <path d={DADA_BODY} fill={`url(#${gradId})`} />
            <g fill="#ff8fa8" opacity="0.38">
              <ellipse cx="35" cy="77" rx="7" ry="4" />
              <ellipse cx="85" cy="77" rx="7" ry="4" />
            </g>
            <ellipse cx="48" cy="62" rx="7" ry="10.2" fill={DADA_EYE} />
            <ellipse cx="72" cy="62" rx="7" ry="10.2" fill={DADA_EYE} />
          </>
        )}

        {role === "verifier" && (
          <>
            <path d="M58 23 C54 11 45 7 40 12 C44 18 52 21 58 23 Z" fill="#16a34a" />
            <path d="M60 23 C65 11 74 7 80 12 C75 18 67 21 60 23 Z" fill="#22c55e" />
            <path d={DADA_BODY} fill={`url(#${gradId})`} />
            <g fill="#ff8fa8" opacity="0.4">
              <ellipse cx="35" cy="77" rx="7" ry="4" />
              <ellipse cx="85" cy="77" rx="7" ry="4" />
            </g>
            <path d="M41 64 Q48 54 55 64" fill="none" stroke={DADA_EYE} strokeWidth="5" strokeLinecap="round" />
            <path d="M65 64 Q72 54 79 64" fill="none" stroke={DADA_EYE} strokeWidth="5" strokeLinecap="round" />
          </>
        )}
      </svg>
    </div>
  );
});
