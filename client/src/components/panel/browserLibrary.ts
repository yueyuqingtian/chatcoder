import { create } from "zustand";
import { api, type BrowserBookmarkOut, type BrowserHistoryOut } from "../../api/client";

interface BrowserLibrary {
  bookmarks: BrowserBookmarkOut[];
  history: BrowserHistoryOut[];
  bookmarkBar: boolean;
  refreshBookmarks: () => Promise<void>;
  refreshHistory: () => Promise<void>;
  toggleBookmarkBar: () => void;
}

const BAR_KEY = "chatcoder.browser.bookmarkBar";
function readBarPreference() {
  try { return localStorage.getItem(BAR_KEY) !== "hidden"; } catch { return true; }
}

export const useBrowserLibrary = create<BrowserLibrary>((set, get) => {
  let bookmarkRequest = 0;
  let historyRequest = 0;
  return {
    bookmarks: [],
    history: [],
    bookmarkBar: readBarPreference(),
    refreshBookmarks: async () => {
      const request = ++bookmarkRequest;
      const { items } = await api.listBrowserBookmarks();
      if (request === bookmarkRequest) set({ bookmarks: items });
    },
    refreshHistory: async () => {
      const request = ++historyRequest;
      const { items } = await api.listBrowserHistory(undefined, 100);
      if (request === historyRequest) set({ history: items });
    },
    toggleBookmarkBar: () => {
      const bookmarkBar = !get().bookmarkBar;
      set({ bookmarkBar });
      try { localStorage.setItem(BAR_KEY, bookmarkBar ? "shown" : "hidden"); } catch { /* 仅本次生效 */ }
    },
  };
});

export function isBrowserHttpUrl(value: string) {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password;
  } catch { return false; }
}

export function browserSearchTarget(value: string) {
  const query = value.trim();
  if (!query || /^(?:file:|https?:\/\/|localhost(?::|\/|$)|\[::1\]|(?:\d{1,3}\.){3}\d{1,3}(?:[/:?#]|$)|[\w.-]+:\d+|[^\s/]+\.[a-z\d]{2,}(?:[/:?#]|$))/i.test(query)) {
    return query;
  }
  return `https://www.bing.com/search?q=${encodeURIComponent(query)}`;
}
