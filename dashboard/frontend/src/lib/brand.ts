// The app's brand in one place. Deliberately game-neutral.
import i18n from "../i18n";

export const APP_NAME = "Stacks Console";

/** The tagline under the name, in the active language. A function, not a constant: a module-level
 *  constant would freeze the language at import. */
export function appSub(): string {
  return i18n.t("common:brand.tagline");
}
