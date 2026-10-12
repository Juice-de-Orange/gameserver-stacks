// A label in the typography of the game — narrow and heavy for Zomboid, wide-spaced for
// Factorio, stretched far apart for Valheim. The values come from the `--g-label-*` tokens
// (`index.css`); this file only says WHERE they apply.
//
// That is half of the art style: a surface with a different pattern looks different, a surface
// with a different type width feels different.
import { ElementType, ReactNode } from "react";
import { cx } from "../ui";

export function GameLabel({
  as: Tag = "div",
  className,
  children,
}: {
  as?: ElementType;
  className?: string;
  children: ReactNode;
}) {
  return <Tag className={cx("g-label", className)}>{children}</Tag>;
}
