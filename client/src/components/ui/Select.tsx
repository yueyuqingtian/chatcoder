/** Select - 统一下拉选择 */
import type { SelectHTMLAttributes } from "react";
export function Select({ className = "", children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={"ui-select " + className} {...props}>{children}</select>;
}