from supabase import create_client, Client
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class SupabaseClient:
    def __init__(self):
        self.url = os.environ.get('SUPABASE_URL')
        self.key = os.environ.get('SUPABASE_KEY')
        
        if not self.url or not self.key:
            print("⚠️  SUPABASE_URL and SUPABASE_KEY not set in environment variables")
            print("📝 Please add them to your .env file")
            self.client = None
        else:
            try:
                self.client = create_client(self.url, self.key)
                print("✅ Supabase client initialized successfully")
            except Exception as e:
                print(f"❌ Failed to initialize Supabase: {e}")
                self.client = None
    
    def get_client(self) -> Client:
        return self.client

# Create a singleton instance
supabase_client = SupabaseClient()
supabase = supabase_client.get_client()