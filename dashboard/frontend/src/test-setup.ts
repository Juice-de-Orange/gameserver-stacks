// Tests run in English regardless of the machine's locale: assertions name the source text.
import { configure } from "@testing-library/react";

import i18n from "./i18n";

void i18n.changeLanguage("en");

// Many component tests run the whole app against a fake backend: the first element of a test, or
// the next page after a click, appears only after several requests and a render. On an idle
// machine that takes most of a second — the whole default of `findBy*`/`waitFor` — so with every
// core busy (default workers on a loaded host or CI runner) they failed at random. One limit for
// every wait instead of a constant per file; a wait that succeeds returns as soon as it does.
configure({ asyncUtilTimeout: 15000 });
