def subtract(intervals,cuts):
    result=[]
    for a,b in intervals:
        for c,d in cuts:
            result.extend([(a,c),(d,b)])
    return result
