/// <reference types="vite/client" />

interface Window {
  NOTESGRAPH_CONFIG?: {
    supabaseUrl?: string;
    supabasePublishableKey?: string;
    apiUrl?: string;
  };
}
