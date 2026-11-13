from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, field_validator
import math
import traceback
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Float, DateTime, Boolean, insert, select, update
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime

DATABASE_URL = "sqlite:///./safeboda.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
metadata = MetaData()

# 1. Rides Table
rides_table = Table(
    "rides",
    metadata,
    Column("id", Integer, primary_key=True, index=True),
    Column("rider_id", String, index=True),
    Column("start_lat", Float),
    Column("start_lng", Float),
    Column("end_lat", Float),
    Column("end_lng", Float),
    Column("distance_km", Float),
    Column("estimated_fare_ugx", Float),
    Column("status", String, default="REQUESTED"),
    Column("driver_id", Integer, nullable=True),
    Column("created_at", DateTime, default=datetime.utcnow)
)

# 2. Drivers Table
drivers_table = Table(
    "drivers",
    metadata,
    Column("id", Integer, primary_key=True, index=True),
    Column("name", String),
    Column("phone", String),
    Column("lat", Float),
    Column("lng", Float),
    Column("is_available", Boolean, default=True)
)

metadata.create_all(bind=engine)

app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Dedicated Haversine distance calculator in kilometers
def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0  # Earth radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = (math.sin(dlat / 2.0) ** 2 + 
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2)
    a = max(0.0, min(1.0, a))
    
    c = 2.0 * math.asin(math.sqrt(a))
    return R * c

# Seed mock drivers safely on startup
def seed_initial_drivers():
    db = SessionLocal()
    try:
        existing = db.execute(select(drivers_table)).fetchall()
        if not existing:
            sample_drivers = [
                {"name": "Musa SafeBoda", "phone": "+256770000001", "lat": 0.3476, "lng": 32.6083, "is_available": True}, # Ntinda
                {"name": "Katende Boda", "phone": "+256770000002", "lat": 0.3333, "lng": 32.5682, "is_available": True}, # Makerere
                {"name": "Okello Rider", "phone": "+256770000003", "lat": 0.3136, "lng": 32.5811, "is_available": True}, # City Center
            ]
            db.execute(insert(drivers_table), sample_drivers)
            db.commit()
    finally:
        db.close()

seed_initial_drivers()

class RideRequest(BaseModel):
    rider_id: str = "rider_123"
    start_lat: float
    start_lng: float
    end_lat: float
    end_lng: float

    @field_validator('start_lat', 'end_lat')
    @classmethod
    def validate_latitude(cls, v: float) -> float:
        if abs(v) > 5.0:
            raise ValueError(f"Invalid latitude ({v}). Uganda latitudes are around 0.3 or 0.04.")
        return v

class StatusUpdate(BaseModel):
    status: str

@app.get("/")
def read_root():
    return {"message": "SafeBoda Clone API is running live with Filterable Ride History!"}

@app.post("/api/rides/request")
def request_ride(data: RideRequest, db: Session = Depends(get_db)):
    try:
        distance_km = calculate_haversine_distance(data.start_lat, data.start_lng, data.end_lat, data.end_lng)
        estimated_fare = 2000.0 + (distance_km * 1500.0)

        stmt = insert(rides_table).values(
            rider_id=data.rider_id,
            start_lat=data.start_lat,
            start_lng=data.start_lng,
            end_lat=data.end_lat,
            end_lng=data.end_lng,
            distance_km=round(distance_km, 3),
            estimated_fare_ugx=round(estimated_fare, -2),
            status="REQUESTED",
            driver_id=None,
            created_at=datetime.utcnow()
        )
        result = db.execute(stmt)
        db.commit()
        
        inserted_id = result.inserted_primary_key[0]

        return {
            "ride_id": inserted_id,
            "rider_id": data.rider_id,
            "distance_km": round(distance_km, 3),
            "estimated_fare_ugx": round(estimated_fare, -2),
            "status": "REQUESTED"
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rides/{ride_id}/match")
def match_driver_to_ride(ride_id: int, db: Session = Depends(get_db)):
    ride_row = db.execute(select(rides_table).where(rides_table.c.id == ride_id)).fetchone()
    if not ride_row:
        raise HTTPException(status_code=404, detail=f"Ride ID {ride_id} not found.")
    
    ride = dict(ride_row._mapping)
    if ride["status"] != "REQUESTED":
        raise HTTPException(status_code=400, detail=f"Cannot match driver. Ride status is '{ride['status']}'.")

    driver_rows = db.execute(select(drivers_table).where(drivers_table.c.is_available == True)).fetchall()
    if not driver_rows:
        raise HTTPException(status_code=404, detail="No available drivers found right now (all are busy).")

    closest_driver = None
    min_distance = float('inf')

    for row in driver_rows:
        driver = dict(row._mapping)
        distance = calculate_haversine_distance(ride["start_lat"], ride["start_lng"], driver["lat"], driver["lng"])

        if distance < min_distance:
            min_distance = distance
            closest_driver = driver

    db.execute(update(rides_table).where(rides_table.c.id == ride_id).values(driver_id=closest_driver["id"], status="ACCEPTED"))
    db.execute(update(drivers_table).where(drivers_table.c.id == closest_driver["id"]).values(is_available=False))
    db.commit()

    return {
        "ride_id": ride_id,
        "status": "ACCEPTED",
        "assigned_driver": {
            "driver_id": closest_driver["id"],
            "name": closest_driver["name"],
            "phone": closest_driver["phone"],
            "distance_to_pickup_km": round(min_distance, 3)
        },
        "message": f"Successfully matched with {closest_driver['name']}! Driver is now marked busy."
    }

@app.get("/api/drivers")
def get_drivers(db: Session = Depends(get_db)):
    result = db.execute(select(drivers_table)).fetchall()
    return [dict(row._mapping) for row in result]

@app.get("/api/drivers/{driver_id}/earnings")
def get_driver_earnings(driver_id: int, db: Session = Depends(get_db)):
    driver_row = db.execute(select(drivers_table).where(drivers_table.c.id == driver_id)).fetchone()
    if not driver_row:
        raise HTTPException(status_code=404, detail=f"Driver ID {driver_id} not found.")
    driver = dict(driver_row._mapping)

    completed_rides = db.execute(
        select(rides_table).where(
            (rides_table.c.driver_id == driver_id) & 
            (rides_table.c.status == "COMPLETED")
        )
    ).fetchall()

    total_trips = len(completed_rides)
    total_earnings_ugx = sum(row.estimated_fare_ugx for row in completed_rides)
    total_distance_km = sum(row.distance_km for row in completed_rides)

    return {
        "driver_id": driver["id"],
        "name": driver["name"],
        "phone": driver["phone"],
        "completed_trips": total_trips,
        "total_distance_km": round(total_distance_km, 3),
        "total_earnings_ugx": round(total_earnings_ugx, -2)
    }

@app.post("/api/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, db: Session = Depends(get_db)):
    ride_row = db.execute(select(rides_table).where(rides_table.c.id == ride_id)).fetchone()
    if not ride_row:
        raise HTTPException(status_code=404, detail=f"Ride ID {ride_id} not found.")
    
    ride = dict(ride_row._mapping)

    if ride["status"] in ["COMPLETED", "CANCELLED"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot cancel this ride because its status is already '{ride['status']}'."
        )

    if ride["driver_id"] is not None:
        db.execute(
            update(drivers_table)
            .where(drivers_table.c.id == ride["driver_id"])
            .values(is_available=True)
        )

    db.execute(
        update(rides_table)
        .where(rides_table.c.id == ride_id)
        .values(status="CANCELLED")
    )
    db.commit()

    return {
        "ride_id": ride_id,
        "status": "CANCELLED",
        "freed_driver_id": ride["driver_id"],
        "message": "Ride successfully cancelled. Assigned driver (if any) is now available again."
    }

@app.patch("/api/rides/{ride_id}/status")
def update_ride_status(ride_id: int, data: StatusUpdate, db: Session = Depends(get_db)):
    valid_statuses = ["REQUESTED", "ACCEPTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
    new_status = data.status.upper()
    
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Choose from: {valid_statuses}")
    
    ride_row = db.execute(select(rides_table).where(rides_table.c.id == ride_id)).fetchone()
    if not ride_row:
        raise HTTPException(status_code=404, detail=f"Ride ID {ride_id} not found.")
    
    existing_ride = dict(ride_row._mapping)

    if new_status in ["COMPLETED", "CANCELLED"] and existing_ride["driver_id"]:
        db.execute(update(drivers_table).where(drivers_table.c.id == existing_ride["driver_id"]).values(is_available=True))

    db.execute(update(rides_table).where(rides_table.c.id == ride_id).values(status=new_status))
    db.commit()

    return {
        "ride_id": ride_id,
        "updated_status": new_status,
        "message": f"Ride status successfully updated to {new_status}!"
    }

@app.get("/api/rides/history")
def get_ride_history(rider_id: str = None, status: str = None, db: Session = Depends(get_db)):
    """Retrieve ride history with optional filtering by rider_id and status"""
    stmt = select(rides_table)
    
    if rider_id:
        stmt = stmt.where(rides_table.c.rider_id == rider_id)
    if status:
        stmt = stmt.where(rides_table.c.status == status.upper())
        
    result = db.execute(stmt).fetchall()
    return [dict(row._mapping) for row in result]