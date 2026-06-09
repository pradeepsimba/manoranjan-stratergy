package org.example.hellofx;

public class AtmOption {
    public int    strike;
    public String type;           // CE or PE
    public double entryPrice;
    public double entryPremium;
    public double currentPremium;
    public String expiryDate;
    public double iv;
    public double delta;
    public double theta;

    public double pnlPts()  { return currentPremium - entryPremium; }
    public double pnlRs()   { return pnlPts() * AppConfig.LOT_SIZE; }
    public double entryCost(){ return entryPremium * AppConfig.LOT_SIZE; }
}
