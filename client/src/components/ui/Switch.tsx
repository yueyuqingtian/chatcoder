/** Switch - 统一开关 */
interface SwitchProps { checked: boolean; onChange: (v: boolean) => void; }
export function Switch({ checked, onChange }: SwitchProps) {
  return (
    <label className="ui-switch">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="ui-switch-track" />
    </label>
  );
}