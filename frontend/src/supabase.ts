import { createClient } from "@supabase/supabase-js";

export const supabaseUrl =
  window.NOTESGRAPH_CONFIG?.supabaseUrl || import.meta.env.VITE_SUPABASE_URL;
export const supabasePublishableKey = (
  window.NOTESGRAPH_CONFIG?.supabasePublishableKey ||
  import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY ||
  ""
).trim();

export const supabaseConfigError = !supabaseUrl
  ? "VITE_SUPABASE_URL is missing from frontend/.env."
  : !supabasePublishableKey
    ? "VITE_SUPABASE_PUBLISHABLE_KEY is missing. Copy the publishable key from your Supabase project's Connect dialog."
    : !supabasePublishableKey.startsWith("sb_publishable_")
      ? "VITE_SUPABASE_PUBLISHABLE_KEY must be the new sb_publishable_ key for this Supabase project, not an anon or service_role key."
      : "";

export const supabase = supabaseConfigError
  ? null
  : createClient(supabaseUrl, supabasePublishableKey);
