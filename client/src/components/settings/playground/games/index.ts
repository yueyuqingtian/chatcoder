/** 世界 → 玩法（plan-playground-v2）：六个主题世界各有一种玩法。 */
import { createBreeze } from "./breeze";
import { createCampfire } from "./campfire";
import { createForest } from "./forest";
import { createGraphite } from "./graphite";
import { createSakura } from "./sakura";
import { createTide } from "./tide";
import type { GameFactory } from "./types";

export const GAMES: Record<string, GameFactory> = {
  graphite: createGraphite,
  forest: createForest,
  campfire: createCampfire,
  breeze: createBreeze,
  tide: createTide,
  sakura: createSakura,
};
