def windows(items,width,step=1):
    data=list(items)
    return iter(tuple(data[i:i+width]) for i in range(0,len(data)-width+1))
