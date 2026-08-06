from supabase_client import supabase
import os
from dotenv import load_dotenv

load_dotenv()

def test_supabase_connection():
    try:
        if not supabase:
            print("❌ Supabase client is not initialized")
            return False
        
        # Test connection by trying to get tables
        response = supabase.table('users').select('count').execute()
        print("✅ Supabase connection successful!")
        return True
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        return False

def get_supabase_credentials():
    print("\n📋 Supabase Configuration:")
    print(f"URL: {os.environ.get('SUPABASE_URL')}")
    print(f"Key: {os.environ.get('SUPABASE_KEY')[:10]}... (truncated)")
    return True

if __name__ == '__main__':
    print("Testing Supabase Configuration...")
    test_supabase_connection()
    get_supabase_credentials()